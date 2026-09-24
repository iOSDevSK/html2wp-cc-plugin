# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
"""Gate B and gate C, told apart in verify-wp.py's one report.

verify-wp.py writes the pages' pixels (gate B) and its functional checks
(gate C: routing, menus wired, blog fidelity, collections, stored sources,
SEO) into one report with one `passed`. Read as one verdict, a red picture
was reported to the service as a red C, and a menu that was not wired hid
behind "a picture differs". write-result.py and send-verdicts.sh both split
it here, the same way:

- B is red when a page cell (page × width) has ok false for its pixels;
- C is red when a check says ok/passed false, lists something missing,
  empty or unmatched, or a page cell failed for a reason no picture explains
  (the page does not load the site's stylesheet);
- a red report that neither explains is C — never a picture.
"""

# A page cell that failed for a reason no picture explains (verify-wp.py
# missing_sheets): the page does not load the site's own stylesheet.
FUNCTIONAL_PAGE_STATUS = {"stylesheet-missing"}


def functional_failures(report):
    """The names of what made the report red besides its pixels."""
    found = []
    for name, check in ((report or {}).get("checks") or {}).items():
        if not isinstance(check, dict):
            continue
        if check.get("ok") is False or check.get("passed") is False:
            found.append(name)
        elif any(isinstance(check.get(k), list) and check.get(k) for k in ("missing", "empty", "unmatched")):
            found.append(name)
    for page, widths in ((report or {}).get("pages") or {}).items():
        if isinstance(widths, dict) and any(isinstance(c, dict) and c.get("status") in FUNCTIONAL_PAGE_STATUS
                                            for c in widths.values()):
            found.append(f"{page}: stylesheet missing")
    return found


def pixel_cells(report):
    """(red cells, measured cells, exempt cells, worst diff %, red pages) of
    the pixel comparison; an ok None cell is an exemption the gate made (a
    dynamic listing, a WooCommerce page), not a measurement."""
    red, measured, exempt, worst, pages = 0, 0, 0, 0.0, []
    for page, widths in ((report or {}).get("pages") or {}).items():
        if not isinstance(widths, dict):
            continue
        bad = False
        for cell in widths.values():
            if not isinstance(cell, dict) or "ok" not in cell:
                continue
            if cell.get("ok") is None:
                exempt += 1
                continue
            if cell.get("status") in FUNCTIONAL_PAGE_STATUS:
                continue
            measured += 1
            if cell.get("ok") is False:
                red += 1
                bad = True
                ratio = cell.get("diffRatio")
                if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
                    worst = max(worst, ratio * 100)
        if bad:
            pages.append(page)
    return red, measured, exempt, worst, pages


def split(report):
    """{'b': bool passed, 'c': bool passed, 'pixelPages', 'functional', 'unexplained'}."""
    _, _, _, _, pages = pixel_cells(report)
    functional = functional_failures(report)
    unexplained = (report or {}).get("passed") is not True and not pages and not functional
    return {"b": not pages, "c": not functional and not unexplained,
            "pixelPages": pages, "functional": functional, "unexplained": unexplained}
