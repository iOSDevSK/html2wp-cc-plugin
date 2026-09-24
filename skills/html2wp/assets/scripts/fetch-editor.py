#!/usr/bin/env python3
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
"""Visual Edit Lite, the free click-to-edit editor, from its public release.

    fetch-editor.py {workspace} [--out {workspace}/visual-edit-lite.zip]

Downloads the latest release of https://github.com/iOSDevSK/visual-edit-lite
(the one link SKILL.md recommends to every owner), checks it is the plugin
and not an error page — the archive holds visual-edit-lite/visual-edit-lite.php
with a Plugin Name header, 25 MB at most, from the project's own release
download address — and writes it where install-theme.py --editor takes it.
Prints the release tag. Nothing is installed here.

H2WP_VE_LITE_ZIP names a ZIP a UI already staged (the desktop app puts the
newest release there before every run): it is checked the same way and used
instead of a download. Named but missing or not the plugin, nothing is
downloaded — the UI decides which editor the owner gets — and the run goes on
without it.

Best effort by design: without it the theme still installs and converts; the
editor checks (smoke-editor.py) then have no editor to drive, and the report
says so. Exit 0 = downloaded, 1 = not (the reason on stderr), 2 = usage.

Carried over from the desktop app (src-tauri/src/editor.rs), which fetched it
the same way for the owner's preview.
"""
import argparse
import io
import json
import os
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

RELEASES = "https://api.github.com/repos/iOSDevSK/visual-edit-lite/releases/latest"
DOWNLOADS = "https://github.com/iOSDevSK/visual-edit-lite/releases/download/"
PLUGIN_FILE = "visual-edit-lite/visual-edit-lite.php"
MAX_BYTES = 25 * 1024 * 1024


def valid_archive(data):
    """A real Visual Edit Lite plugin archive, not an HTML error page."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            head = archive.read(PLUGIN_FILE)[:4096].decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError, OSError):
        return False
    return "Plugin Name" in head


def safe_tag(tag):
    return bool(re.fullmatch(r"[A-Za-z0-9.-]{1,40}", tag or ""))


def pick_asset(release):
    """(tag, url) of the release's plugin ZIP, or raise ValueError saying why not."""
    tag = str(release.get("tag_name") or "")
    if not safe_tag(tag):
        raise ValueError("the release has no usable version")
    asset = next((a for a in release.get("assets") or []
                  if str(a.get("name", "")).startswith("visual-edit-lite") and str(a.get("name", "")).endswith(".zip")), None)
    if asset is None:
        raise ValueError("the release has no plugin ZIP")
    if int(asset.get("size") or MAX_BYTES + 1) > MAX_BYTES:
        raise ValueError("the plugin ZIP is unexpectedly large")
    url = str(asset.get("browser_download_url") or "")
    if not url.startswith(DOWNLOADS):
        raise ValueError("the download address is not the official release")
    return tag, url


def plugin_version(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            head = archive.read(PLUGIN_FILE)[:4096].decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError, OSError):
        return None
    m = re.search(r"^[ \t*#@]*Version:\s*(\S+)", head, re.M | re.I)
    return m.group(1) if m else None


def get(url, accept=None):
    request = urllib.request.Request(url, headers={"User-Agent": "html2wp-skill", **({"Accept": accept} if accept else {})})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read(MAX_BYTES + 1)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("workspace")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    out = Path(args.out) if args.out else Path(args.workspace) / "visual-edit-lite.zip"
    staged = os.environ.get("H2WP_VE_LITE_ZIP", "")
    if staged:
        try:
            data = Path(staged).read_bytes() if Path(staged).is_file() and not Path(staged).is_symlink() else b""
        except OSError:
            data = b""
        if len(data) > MAX_BYTES or not valid_archive(data):
            print(f"fetch-editor: H2WP_VE_LITE_ZIP ({staged}) is missing or not the Visual Edit Lite plugin — "
                  "going on without the editor", file=sys.stderr)
            return 1
        if Path(staged).resolve() != out.resolve():
            partial = out.with_suffix(".part")
            partial.write_bytes(data)
            partial.replace(out)
        print(json.dumps({"tag": plugin_version(data), "file": str(out), "bytes": len(data), "source": "H2WP_VE_LITE_ZIP"}))
        return 0
    try:
        tag, url = pick_asset(json.loads(get(RELEASES, "application/vnd.github+json")))
        data = get(url)
        if len(data) > MAX_BYTES or not valid_archive(data):
            raise ValueError("the downloaded ZIP is not a valid plugin")
    except (OSError, ValueError) as error:
        print(f"fetch-editor: Visual Edit Lite not downloaded — {error}", file=sys.stderr)
        return 1
    partial = out.with_suffix(".part")
    partial.write_bytes(data)
    partial.replace(out)
    print(json.dumps({"tag": tag, "file": str(out), "bytes": len(data)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
