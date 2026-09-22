"""Where a conversion manifest's paths really point.

A workspace is the directory that holds its conversion-manifest.json. The
manifest also RECORDS that directory ("workspace") and its input ("input.dir")
as absolute paths — so a copied workspace (a scratch run, a second agent, a
backup) used to read and write the ORIGINAL through them: a stage run on the
copy rewrote the original's theme. The manifest's own location wins; an
input.dir inside the recorded workspace moves with it; a relative one is
relative to the workspace; an input elsewhere stays where it is.

Mirrored by lib/manifest-paths.mjs — the same rule for the JS stages.
"""
from pathlib import Path


def workspace_of(mf, manifest_path):
    """The workspace: the directory of its conversion-manifest.json. A manifest
    under any other name (a fixture, a hand-made one) keeps its recorded
    workspace."""
    here = Path(manifest_path).resolve()
    if here.name == "conversion-manifest.json" or not mf.get("workspace"):
        return here.parent
    return Path(mf["workspace"]).resolve()


def input_dir_of(mf, manifest_path):
    """input.dir, resolved against the workspace the manifest is actually in."""
    ws = workspace_of(mf, manifest_path)
    raw = ((mf.get("input") or {}).get("dir") or "").strip()
    if not raw:
        return ws / "static-src"
    p = Path(raw)
    if not p.is_absolute():
        return (ws / p).resolve()
    declared = Path(mf["workspace"]).resolve() if mf.get("workspace") else None
    p = p.resolve()
    if declared and declared != ws:
        try:
            return ws / p.relative_to(declared)
        except ValueError:
            pass
    return p
