#!/usr/bin/env python3
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
"""The owner's look: every source page beside its WordPress page, on demand.

    visual-compare.py {workspace} [--desktop-only] [--jobs N] [--wp URL] [--target astro]

A UI's "Compare" button runs this directly in the project container — no AI
turn. It is compare-pages.py (stage 5.5's side-by-side composites) run for the
preview WordPress the conversion installed, at 1440 and at 390, with an index
a UI can list and a status file it can poll:

    {workspace}/visual-review/<key>.side-by-side.png          desktop (1440)
    {workspace}/visual-review/mobile/<key>.side-by-side.png   mobile (390)
    {workspace}/visual-review/visual-compare.json   index: key, title, page,
                                                    route, images, diff %, heights
    {workspace}/visual-review/status.json           running | done | failed

The diff % is read off each composite (the share of pixels that differ, the
height difference counted as differing): a hint for which page to open first,
never a gate — the gates are the verdict, and this changes nothing of the
run: not progress.json, not result.json, not a report.

The preview is the one test-env.sh started for this workspace (its state file
names it). Down — a stopped container, a restarted project container — it is
brought back with `test-env.sh up <slug>`, which reuses the same WordPress and
its content. With no preview at all (the theme was never installed) there is
nothing to compare, and the status says so. Visual Edit Lite loads public
scripts, so it is switched off for the capture and back on after, as every
gate does. --wp compares against another address (and skips test-env).

The Astro run (result.json target "astro", or --target astro) has no
WordPress: its right-hand side is the built Astro site
(astro-project/dist), served locally — every page of the manifest, the
original beside the Astro page it became, at the same widths, in the same
index (`"target": "astro"`, `"builtSite"` in place of `"preview"`).

Exit 0 = done; 1 = failed (status.json says why); 2 = usage. A second run while
one is going is refused (exit 1) rather than raced.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA = "h2wp-visual-compare/1"
CAPTION, GAP = 44, 24          # compare-pages.py compose(): caption band, gutter
WIDTHS = (("desktop", 1440, ""), ("mobile", 390, "mobile"))


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_json(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def diff_percent(composite, width, heights):
    """The share of the page that differs between the two halves of a
    compare-pages.py composite (original left, WordPress right, each
    `heights` tall): pixels whose colour moved by more than a little, plus
    the rows one side has and the other does not."""
    from PIL import Image, ImageChops
    board = Image.open(composite).convert("RGB")
    left_h, right_h = heights
    common, tall = min(left_h, right_h), max(left_h, right_h) or 1
    if common <= 0:
        return 100.0
    a = board.crop((0, CAPTION, width, CAPTION + common))
    b = board.crop((width + GAP, CAPTION, 2 * width + GAP, CAPTION + common))
    moved = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 24 else 0)
    changed = moved.histogram()[255] + (tall - common) * width
    return round(100.0 * changed / (tall * width), 2)


def answers(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=10) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except OSError:
        return False


def preview(ws, slug, status):
    """(url, wp-cli prefix) of the workspace's preview, up."""
    state_path = ws / f".test-env-{slug}.json"
    if not state_path.is_file():
        raise RuntimeError("no preview WordPress for this project yet: the theme was never installed "
                           "(the conversion starts one at stage 3)")
    state = json.loads(state_path.read_text())
    if not answers(state["url"] + "/"):
        status("bringing the preview WordPress back up")
        up = subprocess.run(["bash", str(HERE / "test-env.sh"), "up", slug], cwd=ws,
                            capture_output=True, text=True, timeout=900)
        state = json.loads(state_path.read_text())
        if up.returncode != 0 or not answers(state["url"] + "/"):
            tail = (up.stdout + up.stderr).strip().splitlines()[-3:]
            raise RuntimeError("the preview WordPress did not come back: " + " | ".join(tail))
    return state["url"], state.get("wpCli") or ""


def lite(wp_cli, action):
    """Visual Edit Lite's state before, and switch it (best effort)."""
    if not wp_cli:
        return None
    base = shlex.split(wp_cli)
    got = subprocess.run(base + ["plugin", "get", "visual-edit-lite", "--field=status"],
                         capture_output=True, text=True, timeout=60)
    before = got.stdout.strip() if got.returncode == 0 else None
    if action and before:
        subprocess.run(base + ["plugin", action, "visual-edit-lite"], capture_output=True, text=True, timeout=60)
    return before


def serve(directory):
    import functools
    import http.server
    import threading
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def dist_path(dist, page_file):
    """Where the Astro build wrote a page: about.html, or about/index.html."""
    stem = page_file[:-5] if page_file.endswith(".html") else page_file
    for cand in (page_file, f"{stem}/index.html", f"{stem}.html"):
        if (dist / cand).is_file():
            return cand
    return None


def compose(left, right, out_path, title):
    from PIL import Image, ImageDraw
    l, r = Image.open(left).convert("RGB"), Image.open(right).convert("RGB")
    board = Image.new("RGB", (l.width + r.width + GAP, max(l.height, r.height) + CAPTION), (24, 24, 24))
    board.paste(l, (0, CAPTION))
    board.paste(r, (l.width + GAP, CAPTION))
    draw = ImageDraw.Draw(board)
    draw.text((8, 12), f"{title} — ORIGINAL {l.width}x{l.height}", fill=(255, 255, 255))
    draw.text((l.width + GAP + 8, 12), f"CONVERTED (Astro) {r.width}x{r.height}", fill=(255, 255, 255))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    board.save(out_path)
    return l.height, r.height


def astro_compare(ws, manifest, original, widths, status, out):
    """The original beside the built Astro site, per page and width."""
    from playwright.sync_api import sync_playwright
    dist = ws / "astro-project" / "dist"
    if not (dist / "index.html").is_file():
        raise RuntimeError("no built Astro site (astro-project/dist) to compare yet")
    index = {"schema": SCHEMA, "target": "astro", "capturedAt": None, "preview": None,
             "builtSite": "astro-project/dist", "original": original, "pages": {}}
    left_srv, left_url = serve(original)
    right_srv, right_url = serve(dist)
    shots = out / ".shots"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for name, width, sub in widths:
                status(f"capturing {name} ({width}px)")
                target = out / sub if sub else out
                page = browser.new_page(viewport={"width": width, "height": 900})
                for p in manifest.get("pages") or []:
                    file, key = p.get("file") or "", p.get("key") or Path(p.get("file") or "page").stem
                    row = index["pages"].setdefault(key, {"key": key, "page": file, "title": p.get("title"),
                                                          "route": None})
                    built = dist_path(dist, file)
                    if not file or not (Path(original) / file).is_file() or not built:
                        row[name] = {"error": "not in the original" if built else "not in the built Astro site"}
                        continue
                    row["route"] = "/" + built
                    pair = []
                    for side, base, rel in (("left", left_url, file), ("right", right_url, built)):
                        page.goto(f"{base}/{rel}", wait_until="networkidle", timeout=60000)
                        page.evaluate("document.fonts && document.fonts.ready")
                        page.wait_for_timeout(400)
                        shot = shots / f"{key}-{width}-{side}.png"
                        shot.parent.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(shot), full_page=True)
                        pair.append(shot)
                    image = target / f"{key}.side-by-side.png"
                    heights = compose(pair[0], pair[1], image, p.get("title") or key)
                    row[name] = {"image": str(image.relative_to(ws)), "diffPercent": diff_percent(image, width, heights),
                                 "origHeight": heights[0], "wpHeight": heights[1]}
                page.close()
            browser.close()
    finally:
        left_srv.shutdown()
        right_srv.shutdown()
    return index


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("workspace")
    ap.add_argument("--desktop-only", action="store_true")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--wp", default="", help="compare against this address instead of the test-env preview")
    ap.add_argument("--target", choices=("auto", "html", "astro"), default="auto",
                    help="astro: the built Astro site instead of WordPress (default: result.json's target)")
    args = ap.parse_args(argv)
    ws = Path(args.workspace).resolve()
    out = ws / "visual-review"
    status_path = out / "status.json"
    manifest_path = ws / "conversion-manifest.json"

    doc = {"schema": SCHEMA + "-status", "state": "running", "startedAt": now(), "updatedAt": now(),
           "note": "", "pid": os.getpid()}
    try:
        old = json.loads(status_path.read_text())
        if old.get("state") == "running" and old.get("pid") and old["pid"] != os.getpid():
            os.kill(int(old["pid"]), 0)
            print(f"visual-compare: a comparison is already running (pid {old['pid']})", file=sys.stderr)
            return 1
    except (OSError, ValueError, ProcessLookupError):
        pass

    def status(note, state="running", **extra):
        doc.update(state=state, note=note, updatedAt=now(), **extra)
        write_json(status_path, doc)
        print(f"visual-compare: {note}")

    status("starting")
    if not manifest_path.is_file():
        status("no conversion-manifest.json: nothing converted to compare yet", "failed")
        return 1
    manifest = json.loads(manifest_path.read_text())
    slug = (manifest.get("site") or {}).get("slug") or ""
    original = (manifest.get("input") or {}).get("dir") or str(ws / "static-src")
    lite_before = None
    wp_cli = ""
    target = args.target
    if target == "auto":
        try:
            target = "astro" if json.loads((ws / "result.json").read_text()).get("target") == "astro" else "html"
        except (OSError, ValueError):
            target = "html"
    if target == "astro":
        try:
            index = astro_compare(ws, manifest, original, WIDTHS[:1] if args.desktop_only else WIDTHS, status, out)
            index["capturedAt"] = now()
            index["pages"] = list(index["pages"].values())
            write_json(out / "visual-compare.json", index)
            made = sum(1 for p in index["pages"] for w in ("desktop", "mobile") if (p.get(w) or {}).get("image"))
            status(f"{made} side-by-side composite(s) of {len(index['pages'])} page(s)", "done",
                   index="visual-review/visual-compare.json")
            return 0
        except Exception as error:  # noqa: BLE001 — a browser error too: the status must say failed
            status(str(error)[:600], "failed")
            return 1
    try:
        wp, wp_cli = (args.wp.rstrip("/"), "") if args.wp else preview(ws, slug, status)
        lite_before = lite(wp_cli, "deactivate")
        index = {"schema": SCHEMA, "capturedAt": None, "preview": wp, "original": original, "pages": {}}
        widths = WIDTHS[:1] if args.desktop_only else WIDTHS
        for name, width, sub in widths:
            status(f"capturing {name} ({width}px)")
            target = out / sub if sub else out
            run = subprocess.run([sys.executable, "-W", "ignore::SyntaxWarning", str(HERE / "compare-pages.py"),
                                  f"--manifest={manifest_path}", "--wp", wp, "--original", original,
                                  "--out", str(target), "--width", str(width), "--jobs", str(max(1, args.jobs))],
                                 capture_output=True, text=True, timeout=3600)
            review = target / "review-manifest.json"
            if run.returncode != 0 or not review.is_file():
                tail = (run.stdout + run.stderr).strip().splitlines()[-3:]
                raise RuntimeError(f"the {name} capture failed: " + " | ".join(tail))
            titles = {p.get("file"): p.get("title") for p in manifest.get("pages") or []}
            for pair in json.loads(review.read_text()).get("pairs") or []:
                key = pair.get("key") or Path(pair.get("page", "")).stem
                row = index["pages"].setdefault(key, {"key": key, "page": pair.get("page"),
                                                      "title": titles.get(pair.get("page")), "route": pair.get("wpUrl")})
                if "composite" in pair:
                    image = target / pair["composite"]
                    diff = diff_percent(image, width, (pair.get("origHeight") or 0, pair.get("wpHeight") or 0))
                    row[name] = {"image": str(image.relative_to(ws)), "diffPercent": diff,
                                 "origHeight": pair.get("origHeight"), "wpHeight": pair.get("wpHeight")}
                else:
                    row[name] = {"error": pair.get("error") or "not captured"}
        index["capturedAt"] = now()
        index["pages"] = list(index["pages"].values())
        write_json(out / "visual-compare.json", index)
        made = sum(1 for p in index["pages"] for w in ("desktop", "mobile") if (p.get(w) or {}).get("image"))
        status(f"{made} side-by-side composite(s) of {len(index['pages'])} page(s)", "done",
               index="visual-review/visual-compare.json")
        return 0
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        status(str(error)[:600], "failed")
        return 1
    finally:
        if lite_before == "active":
            lite(wp_cli, "activate")


if __name__ == "__main__":
    sys.exit(main())
