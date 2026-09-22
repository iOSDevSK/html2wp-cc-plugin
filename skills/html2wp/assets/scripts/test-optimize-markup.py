#!/usr/bin/env python3
"""Offline test: optimize-markup.py reaches every page, at any depth.

A prerendered app writes nested routes (`product/<slug>.html`,
`journal/2026/<slug>.html`). The stage used to glob only the directory's top
level, so on a 22-page shop 16 pages — every product and article — were
skipped without a word. The cases vary the depth, the entry point (--input
vs --manifest) and whether --responsive measures in a browser.

  python3 assets/scripts/test-optimize-markup.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "optimize-markup.py"

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
.thumb{width:120px}</style></head><body><main>
%s
</main></body></html>"""


def img(src, cls=""):
    return f'<img src="{src}" alt=""{f" class={chr(34)}{cls}{chr(34)}" if cls else ""}>'


def site(root, pages):
    (root / "assets").mkdir(parents=True)
    Image.new("RGB", (1600, 1200), (180, 120, 90)).save(root / "assets" / "big.png")
    Image.new("RGB", (400, 300), (90, 120, 180)).save(root / "assets" / "small.png")
    for rel, body in pages.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(PAGE % body)


def run(args):
    proc = subprocess.run([sys.executable, str(SCRIPT), *args, "--apply"],
                          capture_output=True, text=True, timeout=300)
    return proc


def sized(root, rel):
    """How many <img> on the page carry width= after the run."""
    return (root / rel).read_text().count('width="')


def main():
    failures = []

    def check(name, cond, detail):
        print(("ok   " if cond else "FAIL ") + name + ("" if cond else f" — {detail}"))
        if not cond:
            failures.append(name)

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)

        # 1. bare --input: nested and deeply nested pages are reached
        root = tmp / "a" / "site"
        pages = {
            "index.html": img("assets/big.png"),
            "product/cable-knit.html": img("../assets/big.png") + img("/assets/small.png"),
            "journal/2026/03/deep-post.html": img("../../../assets/small.png"),
        }
        site(root, pages)
        proc = run(["--input", str(root), "--out", str(tmp / "a" / "r.json")])
        rep = json.loads((tmp / "a" / "r.json").read_text()) if proc.returncode == 0 else {}
        check("input: every page reported", set(rep.get("pages", {})) == set(pages),
              f"rc={proc.returncode} reported={sorted(rep.get('pages', {}))} {proc.stderr[-300:]}")
        for rel in pages:
            want = pages[rel].count("<img")
            check(f"input: {rel} sized", sized(root, rel) == want, f"{sized(root, rel)} of {want}")

        # 2. --manifest: the manifest's page list is the authority — a stray
        #    .html that is not a converted page is left alone
        ws = tmp / "b"
        root = ws / "static-src"
        pages = {
            "index.html": img("assets/big.png"),
            "shop/item-one.html": img("../assets/big.png"),
            "stray/not-a-page.html": img("../assets/big.png"),
        }
        site(root, pages)
        manifest = {"input": {"dir": str(root)}, "workspace": str(ws),
                    "pages": [{"file": "index.html"}, {"file": "shop/item-one.html"}]}
        (ws / "conversion-manifest.json").write_text(json.dumps(manifest))
        proc = run(["--manifest", str(ws / "conversion-manifest.json")])
        rep = json.loads((ws / "optimize-markup-report.json").read_text()) if proc.returncode == 0 else {}
        check("manifest: exactly the declared pages", set(rep.get("pages", {})) == {"index.html", "shop/item-one.html"},
              f"rc={proc.returncode} reported={sorted(rep.get('pages', {}))} {proc.stderr[-300:]}")
        check("manifest: nested declared page sized", sized(root, "shop/item-one.html") == 1, "not sized")
        check("manifest: undeclared page untouched", sized(root, "stray/not-a-page.html") == 0, "was edited")

        # 3. --responsive: a nested page's small slot gets its srcset (the
        #    per-use width is keyed by the page's RELATIVE path)
        root = tmp / "c" / "site"
        pages = {
            "index.html": "<p>home</p>",
            "product/thumbs.html": img("../assets/big.png", "thumb"),
        }
        site(root, pages)
        proc = run(["--input", str(root), "--responsive", "--out", str(tmp / "c" / "r.json")])
        html = (root / "product/thumbs.html").read_text()
        check("responsive: nested page carries srcset", proc.returncode == 0 and "srcset=" in html and 'sizes="120px"' in html,
              f"rc={proc.returncode} {html[html.find('<img'):html.find('<img') + 240]} {proc.stderr[-300:]}")

    print(f"{'FAILED' if failures else 'all passed'} ({len(failures)} failure(s))")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
