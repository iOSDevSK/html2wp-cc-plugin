#!/usr/bin/env python3
"""lib/listing_cards.py — gate C6 counts the listing the generator built.

  python3 assets/scripts/test-listing-cards.py
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from listing_cards import count_listing_cards, count_after_paging  # noqa: E402

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
    # a paged listing: 2 cards, a pager that appends 2 more twice, then hides
    page.set_content("""<main><div class="grid"><a>1</a><a>2</a></div>
      <button data-cve-load-kind="products" onclick="const g=document.querySelector('.grid');
        g.insertAdjacentHTML('beforeend','<a>x</a><a>y</a>'); if (g.children.length >= 6) this.hidden = true;">Load more</button></main>""")
    got = count_after_paging(page, "div.grid", settle_ms=50)
    ok = got == (2, 6)
    failed += not ok
    print(("ok   " if ok else "FAIL ") + "a paged listing is paged to the end before it is counted" + ("" if ok else f" — got {got}"))
    page.set_content("<main><div class='grid'><a>1</a></div></main>")
    ok = count_after_paging(page, "div.grid", settle_ms=50) == (1, 1)
    failed += not ok
    print(("ok   " if ok else "FAIL ") + "no pager: one count")
    b.close()
print("FAILED" if failed else "all passed")
sys.exit(1 if failed else 0)
