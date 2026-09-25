# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
"""The delivered theme after delivery: what changed, and against what.

Shared by apply-change.py (a change applied to the running preview) and
package-theme.py (the owner's "Make release"). A delivered project is one whose
{workspace}/result.json says status "delivered"; its theme lives at
{workspace}/theme/<slug>/ and every comparison is by file content:

- the LAST ZIP is the theme file result.json names (in the output directory,
  else the workspace), read entry by entry;
- the LAST APPLIED state is {workspace}/.theme-applied.json (path → sha256),
  written each time a change reached the preview;
- workspace/changes.json is the change log (schema h2wp-changes/1).
"""
import hashlib
import json
import os
import subprocess
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
CHANGES_SCHEMA = "h2wp-changes/1"
# What make-zip.sh leaves out wherever it appears; the rest of its exclusions
# are root build residue a converted theme does not carry.
JUNK = (".DS_Store", "__MACOSX")


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def write_json(path, doc):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def delivered(ws):
    """The workspace's result.json when the run delivered, else None."""
    result = read(Path(ws) / "result.json")
    return result if isinstance(result, dict) and result.get("status") == "delivered" else None


def output_dir(ws, given=""):
    return Path(given or os.environ.get("H2WP_OUTPUT_DIR") or Path(ws) / "out").resolve()


def theme_dir(ws, manifest):
    return Path(ws) / "theme" / ((manifest.get("site") or {}).get("slug") or "")


def tree(theme):
    """{relative path: sha256} of the theme's files, junk left out."""
    out = {}
    for path in sorted(Path(theme).rglob("*")):
        rel = path.relative_to(theme)
        if not path.is_file() or path.is_symlink() or any(p in JUNK or p.startswith("._") for p in rel.parts):
            continue
        out[rel.as_posix()] = sha256(path.read_bytes())
    return out


def zip_tree(zip_path):
    """{relative path: sha256} of a theme ZIP's files, its root folder left out."""
    out = {}
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.is_dir() or "/" not in info.filename:
                continue
            out[info.filename.split("/", 1)[1]] = sha256(archive.read(info))
    return out


def last_zip(ws, result, output):
    """The theme ZIP result.json names, where it is."""
    name = (result.get("theme") or {}).get("file")
    for folder in (output, Path(ws)):
        if name and (folder / name).is_file():
            return folder / name
    return None


def differ(a, b):
    """Paths whose content differs between two {path: sha} maps (added, removed or changed)."""
    return sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))


def make_zip(theme, out, manifest_path):
    """(ok, output) of make-zip.sh — the same packer and refusals as stage 6."""
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    run = subprocess.run(["bash", str(HERE / "make-zip.sh"), str(theme), str(out)], capture_output=True, text=True,
                         env={**os.environ, "MAKE_ZIP_MANIFEST": str(manifest_path)}, timeout=600)
    return run.returncode == 0, (run.stdout + run.stderr).strip()


# What the Astro project ZIP leaves out: installed packages, caches, logs and
# macOS metadata. The desktop app packaged it the same way (files.rs
# zip_astro_project).
ASTRO_SKIPPED = {"node_modules", ".astro", ".cache", ".vite", ".turbo", ".npm", ".git", ".DS_Store", ".html2wp"}


def zip_astro_project(project, dest, top):
    """The generated Astro 5 project as a ZIP under one `{top}/` folder —
    sources, public files, package metadata, config and the built dist/ — with
    the converter's astro-report.json at .html2wp/astro-report.json, so the ZIP
    can come back as a ready Astro input (detect-project.py: html2wp-astro).
    False, and nothing written, when there is no project."""
    if not (project / "package.json").is_file():
        return False
    partial = dest.with_suffix(".part")
    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(project.rglob("*")):
                rel = path.relative_to(project)
                if any(p in ASTRO_SKIPPED or p.startswith("._") or p.endswith(".log") for p in rel.parts):
                    continue
                if path.is_symlink() or not path.is_file():
                    continue
                zf.write(path, f"{top}/{rel.as_posix()}")
            report = project.parent / "astro-report.json"
            if report.is_file() and not report.is_symlink():
                zf.write(report, f"{top}/.html2wp/astro-report.json")
        partial.replace(dest)
    except OSError:
        partial.unlink(missing_ok=True)
        raise
    return True


def astro_tree(project, parts=("src", "public")):
    """{relative path: sha256} of the Astro project's own sources — what a
    change after delivery edits (src/, public/), never the build output."""
    out = {}
    for top in parts:
        base = Path(project) / top
        for path in sorted(base.rglob("*")) if base.is_dir() else ([base] if base.is_file() else []):
            rel = path.relative_to(project)
            if not path.is_file() or path.is_symlink() or any(p in ASTRO_SKIPPED or p.startswith("._") for p in rel.parts):
                continue
            out[rel.as_posix()] = sha256(path.read_bytes())
    return out


def astro_zip_tree(zip_path, parts=None):
    """zip_tree() of an Astro project ZIP without the converter's own
    .html2wp/ entry; `parts` keeps only those top folders."""
    return {k: v for k, v in zip_tree(zip_path).items()
            if not k.startswith(".html2wp/") and (parts is None or k.split("/", 1)[0] in parts)}


def last_astro_zip(ws, result, output):
    """The Astro project ZIP result.json names, where it is."""
    name = (result.get("astro") or {}).get("file")
    for folder in (output, Path(ws)):
        if name and (folder / name).is_file():
            return folder / name
    return None


def load_changes(ws):
    doc = read(Path(ws) / "changes.json")
    if not isinstance(doc, dict) or doc.get("schema") != CHANGES_SCHEMA:
        doc = {"schema": CHANGES_SCHEMA, "changedSinceZip": False, "sinceZip": 0, "lastZip": None, "changes": []}
    return doc
