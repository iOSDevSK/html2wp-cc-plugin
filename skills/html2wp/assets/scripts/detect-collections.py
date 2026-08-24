#!/usr/bin/env python3
"""Stage 2.7 — find every repeating group on every built page, and record it.

  python3 detect-collections.py --manifest=conversion-manifest.json

The Visual Edit plugin detects collections (repeating cards, FAQ items,
feature lists) at RUNTIME, when someone clicks — which means a group it
fails to offer is only ever discovered by the person who clicked, on the
page where they happened to click. This stage closes that gap from the
other side: it runs the plugin's own detection rules (lib/collection-detect.js,
a faithful port of bridge.js) over EVERY built page at conversion time and
writes what it finds into the manifest's `collections` field — until now
documented and read by nothing.

verify-wp.py's collections gate then asserts the live editor will offer
each recorded group with the same item count. A repeating group the editor
will not offer is a FAILURE with the page and group named, not a silent gap
someone's owner finds by clicking.

Pages are loaded with JavaScript DISABLED: the DOM is then the parsed
markup itself — the exact thing the plugin's path stamping addresses
(bridge.js runs before the page's own scripts and snapshots pristine
classes for the same reason). Run AFTER materialize-js-text.py, so copy the
site keeps in JavaScript is already in the markup and detectable.

Excluded, on both sides of the eventual comparison: the manifest's nav
zones (the editor gives those its menu panel, not a collection popup) and
the blog card container (those cards become the dynamic [wp-posts] zone).

Updates the manifest in place — `collections` is mechanical data, measured
not judged — and writes {workspace}/collections-report.json with the same
content plus per-page detail.
"""

import argparse, functools, json, sys, threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent / "lib"))
from collection_coverage import uncovered

ap = argparse.ArgumentParser()
ap.add_argument("--manifest", required=True)
ap.add_argument("--dist", default="")
args = ap.parse_args()

MANIFEST = Path(args.manifest).resolve()
MF = json.loads(MANIFEST.read_text())
WS = Path(MF["workspace"]).resolve()
DIST = Path(args.dist or (WS / "astro-project" / "dist")).resolve()

# From the function expression itself — playwright decides "expression or
# function?" by looking at the string's start, and the file opens with the
# comment block explaining it.
_lib = (Path(__file__).parent / "lib" / "collection-detect.js").read_text()
DETECT_JS = _lib[_lib.index("(config)"):]

base_exclude = [e.get("selector", "") for e in (MF.get("nav") or [])]
card_container = (MF.get("blog") or {}).get("cardContainer")
if card_container:
    base_exclude.append(card_container)
base_exclude = [s for s in base_exclude if s]

# Static dist pages still carry their captured header/footer, but ordinary
# WordPress pages do not own that markup: make-theme renders it from editable
# template parts and dist-to-bundle removes it from the page source. Recording
# a footer list against every page therefore guarantees false Gate C5 failures
# after a correct conversion. The front page always delegates its header; it
# delegates trailing chrome only when frontOwnsFooter is false.
header_selector = ((MF.get("chrome") or {}).get("header") or {}).get("selector", "header")
trailing_selectors = []
for chrome_entry in (MF.get("chrome") or {}).get("trailing", []):
    trailing_selectors.extend(chrome_entry.get("selectors") or [])
if not trailing_selectors:
    trailing_selectors = ["footer"]


def exclude_for_page(entry):
    selectors = list(base_exclude)
    selectors.append(header_selector)
    if entry.get("kind") != "front" or not (MF.get("chrome") or {}).get("frontOwnsFooter", True):
        selectors.extend(trailing_selectors)
    return list(dict.fromkeys(s for s in selectors if s))


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(DIST)))
threading.Thread(target=httpd.serve_forever, daemon=True).start()


# ---------------------------------------------------------------------------
# An INDEPENDENT coverage probe, deliberately not the plugin's rules.
#
# Gate C5 compares what this script recorded against what the live editor
# offers — but BOTH sides run lib/collection-detect.js. When those rules are
# blind to a group, both sides are blind identically, they agree perfectly,
# and the gate is green. It is a consistency check wearing a coverage check's
# clothes: it can catch "the editor lost a group the build had", never
# "neither side ever saw it".
#
# That is not hypothetical. A five-question FAQ accordion — the most obviously
# repeating thing on its page — was missed on a real conversion because the
# open item had frozen widget state in its markup (`aria-disabled`, and
# measured sizes in a `style` custom property) that made it look
# design-different from the closed four. Every gate stayed green.
#
# So this pass looks for repetition a CRUDER way, on purpose: three or more
# contiguous siblings of the same tag that each carry text, ignoring every
# attribute including class. Anything it finds that the real rules did NOT
# record is printed as a candidate for a human to adjudicate. It is a REPORT,
# never a gate — a loose rule produces false positives by design, and the
# answer to a false positive is a person saying "that is not a list", not a
# silently narrowed rule.
LOOSE_JS = """(config) => {
  var excluded = (config.excludeSelectors || []).filter(Boolean).join(', ');
  var out = [];
  var all = document.body ? document.body.querySelectorAll('*') : [];
  for (var p = 0; p < all.length; p++) {
    var parent = all[p];
    if (excluded && parent.closest && parent.closest(excluded)) continue;
    var kids = [];
    for (var k = 0; k < parent.children.length; k++) kids.push(parent.children[k]);
    if (kids.length < 3) continue;
    var i = 0;
    while (i < kids.length) {
      var tag = kids[i].tagName;
      var j = i;
      // A table body can place a section-heading <tr> between two real row
      // runs. Keep the independent probe aligned with the editor's safe
      // maximal runs instead of reporting the headings plus jobs as one
      // uncovered list. Other parents retain the deliberately loose same-tag
      // grouping, including the false positives humans are expected to judge.
      var tableRowKind = tag === 'TR' && parent.tagName === 'TBODY'
        ? Array.from(kids[i].children).map(function (child) { return child.tagName; }).join(',')
        : '';
      while (j < kids.length && kids[j].tagName === tag) {
        if (tableRowKind && Array.from(kids[j].children).map(function (child) { return child.tagName; }).join(',') !== tableRowKind) break;
        j++;
      }
      var run = kids.slice(i, j);
      i = j;
      if (run.length < 3) continue;
      var texted = run.filter(function (e) { return (e.textContent || '').trim().length > 2; });
      if (texted.length !== run.length) continue;
      var texts = run.map(function (e) { return (e.textContent || '').trim(); });
      var varies = texts.some(function (t) { return t !== texts[0]; });
      if (!varies) continue;
      out.push({
        parentTag: parent.tagName.toLowerCase(),
        parentClasses: (parent.getAttribute('class') || '').trim().split(/\\s+/).filter(Boolean).sort().join(' '),
        tag: tag.toLowerCase(),
        count: run.length,
        sample: texts[0].slice(0, 40)
      });
    }
  }
  return out;
}"""


collections = []
per_page = {}
per_page_uncovered = {}
with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1440, "height": 950}, java_script_enabled=False)
    page = ctx.new_page()
    for entry in MF["pages"]:
        f = entry["file"]
        if entry.get("kind") == "article":
            # Becomes a Post — its repeating groups are prose lists inside
            # post content, edited through the article template, never the
            # collections popup. Recording them is noise the gate then has
            # to skip anyway.
            continue
        if not (DIST / f).exists():
            continue
        page.goto(f"http://127.0.0.1:{httpd.server_port}/{f}")
        page_exclude = exclude_for_page(entry)
        found = page.evaluate(DETECT_JS, {"excludeSelectors": page_exclude})
        per_page[f] = found
        gaps = uncovered(page.evaluate(LOOSE_JS, {"excludeSelectors": page_exclude}), found)
        if gaps:
            per_page_uncovered[f] = gaps
        for g in found:
            collections.append({"page": f, **g})
    browser.close()
httpd.shutdown()

MF["collections"] = collections
MANIFEST.write_text(json.dumps(MF, indent=2))
(WS / "collections-report.json").write_text(json.dumps(
    {"baseExcludeSelectors": base_exclude, "pages": per_page, "total": len(collections),
     "uncoveredCandidates": per_page_uncovered}, indent=2))

pages_with = sum(1 for v in per_page.values() if v)
print(f"OK — {len(collections)} repeating group(s) across {pages_with} page(s) recorded into the manifest "
      f"(and {WS / 'collections-report.json'})")
for f, found in per_page.items():
    for g in found[:3]:
        print(f"  {f}: {g['count']}× <{g['memberShape'].split('|')[0]}> in <{g['parentTag']}"
              + (f" class=\"{g['parentClasses']}\"" if g['parentClasses'] else "") + f"> ({g['slots']} slot(s))")

n_gaps = sum(len(v) for v in per_page_uncovered.values())
if n_gaps:
    print(f"\n  {n_gaps} repeating run(s) the editor will NOT offer — read these and decide:")
    for f, gaps in per_page_uncovered.items():
        for g in gaps[:4]:
            print(f"    {f}: {g['count']}x <{g['tag']}> in <{g['parentTag']}"
                  + (f" class=\"{g['parentClasses'][:44]}\"" if g['parentClasses'] else "")
                  + f'> - "{g["sample"]}"')
    print("  Each is either genuinely not a list (fine - say so in the report), or a")
    print("  group the plugin's congruence rules reject. If it is the latter, find the")
    print("  attribute that differs across members and fix lib/collection-detect.js AND")
    print("  bridge.js together - they are one rule set in two files.")
