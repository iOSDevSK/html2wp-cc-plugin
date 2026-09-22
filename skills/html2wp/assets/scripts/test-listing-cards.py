#!/usr/bin/env python3
"""lib/listing_cards.py — gate C6 counts the listing the generator built.

  python3 assets/scripts/test-listing-cards.py
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from listing_cards import count_listing_cards  # noqa: E402

cards = lambda n: "".join(f'<a class="card">{i}</a>' for i in range(n))
CASES = [
    ("footer grid after the listing (Terra Studios)", "div.grid",
     f'<header></header><main><div class="grid">{cards(12)}</div></main><footer><div class="grid">{cards(4)}</div></footer>', 12),
    ("a header grid before <main>", "div.grid",
     f'<header><div class="grid">{cards(3)}</div></header><main><section><div class="grid">{cards(7)}</div></section></main>', 7),
    ("two grids inside main: the first is the listing", "div.grid",
     f'<main><div class="grid">{cards(5)}</div><div class="grid">{cards(3)}</div></main>', 5),
    ("no <main> at all", "ul.products", f'<div><ul class="products">{"<li>x</li>" * 6}</ul></div>', 6),
    ("container only outside main", "div.grid", f'<main><p>empty</p></main><aside><div class="grid">{cards(2)}</div></aside>', 2),
    ("container absent", "div.nope", "<main><div class='grid'></div></main>", 0),
]

failed = 0
with sync_playwright() as p:
    b = p.chromium.launch()
    page = b.new_page()
    for name, sel, body, want in CASES:
        page.set_content(f"<!doctype html><html><body>{body}</body></html>")
        got = count_listing_cards(page, sel)
        ok = got == want
        failed += not ok
        print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" — got {got}, want {want}"))
    b.close()
print("FAILED" if failed else "all passed")
sys.exit(1 if failed else 0)
