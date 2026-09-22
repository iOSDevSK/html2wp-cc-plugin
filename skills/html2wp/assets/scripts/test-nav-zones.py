#!/usr/bin/env python3
"""lib/nav_zones.py — the order gates look for a nav zone in."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from nav_zones import nav_zone_candidates  # noqa: E402

cases = [
    ({"selector": "div.flex-col.gap-3"}, 3, ['[data-ve-nav="4"]', "div.flex-col.gap-3"]),
    ({"selector": "nav.main", "zoneSelector": '[data-ve-nav="1"]'}, 0, ['[data-ve-nav="1"]', "nav.main"]),
    ({"zoneSelector": "nav.x"}, 1, ["nav.x", '[data-ve-nav="2"]']),
    ({}, 0, ['[data-ve-nav="1"]']),
]
bad = 0
for entry, i, want in cases:
    got = nav_zone_candidates(entry, i)
    ok = got == want
    bad += not ok
    print(("ok   " if ok else "FAIL ") + repr(entry) + ("" if ok else f" -> {got}"))
sys.exit(1 if bad else 0)
