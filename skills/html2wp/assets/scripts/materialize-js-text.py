#!/usr/bin/env python3
"""Stage 2.6 — move copy the site keeps in JavaScript into the markup.

  python3 materialize-js-text.py --manifest conversion-manifest.json [--apply]

A component-driven template routinely ships a container empty and lets a
script fill it at runtime: an accordion answer, a tab panel body, a carousel
quote. The conversion carries that faithfully — the script travels with the
theme and the words still appear — but the words are in NO stored source, so
after conversion the owner cannot edit them. Nothing else in the pipeline can
see it: pixel gates match (both sides run the same script), markup gates match
for the same reason, and the editor can only offer fields that exist in the
markup. Verified live: a converted site's FAQ offered its five questions as an
editable list and had nothing at all for the answers.

This closes that gap generically, without knowing anything about the site's
JavaScript — no filename convention, no framework assumption:

  1. Open each built page and let its own scripts run.
  2. Expand everything that can be expanded (details, aria-expanded triggers,
     data-state="closed" regions), so content that only appears on interaction
     is present.
  3. Find every element that is EMPTY in the HTML file but has content in the
     live DOM. Only empty ones — filling a blank is safe, and never touching
     markup that already has content is what keeps this from baking in a
     carousel's current slide or a filtered list's current state.
  4. Write that content into the page's own source fragment.
  5. Report the script literals now duplicated in the markup, so the
     assignment that would overwrite the owner's edit can be removed. With
     --apply, remove them too — and gate A afterwards proves the page still
     renders identically, which is the whole check that the removal was safe.

Without --apply nothing is written; the report alone is worth running.
"""

import argparse, functools, json, re, sys, threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--manifest", required=True)
ap.add_argument("--apply", action="store_true",
                help="write the changes; without it, report only")
ap.add_argument("--out", default=None,
                help="report path; defaults into the manifest's workspace, "
                     "which is where gate A2 reads it from")
args = ap.parse_args()

MF = json.loads(Path(args.manifest).read_text())
WS = Path(MF["workspace"]).resolve()
# Same contract as stage 2.65's report: gate A2 reverses this stage's edits
# from {workspace}/materialize-report.json, so a cwd-relative default hides
# the exemptions from the gate that needs them.
args.out = args.out or str(WS / "materialize-report.json")
DIST = WS / "astro-project" / "dist"
FRAG = WS / "astro-project" / "src" / "fragments"
INPUT = Path(MF["input"]["dir"]).resolve()


def serve(directory):
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(directory)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_port}"


# Everything that hides content behind an interaction, expanded before reading
# the DOM — and read AFTER EACH trigger rather than once at the end. Clicking
# the trigger rather than forcing the attribute, so the page's own handler runs
# and does whatever filling it does.
#
# The read has to interleave with the clicks because an accordion is very often
# SINGLE-SELECT: opening one panel closes the previous one, and the same
# statement that fills a panel on open clears it on close
# (`el.innerHTML = open ? … : ''`). Expanding all five triggers and then
# collecting once therefore sees exactly ONE panel filled — the last clicked —
# and reports the other four as having no JS-held copy at all. Verified live on
# a five-question FAQ: 1 of 5 answers was collected, and applying that would
# have written one answer into the markup and then removed the statement that
# was the only source of the other four, deleting them from the site.
#
# Content is accumulated by the element's original opening tag, first non-empty
# reading wins, so a panel captured while open is kept after it closes again.
EXPAND_AND_COLLECT = """async () => {
  const acc = new Map();
  const grab = () => {
    for (const el of document.querySelectorAll('[data-cve-was-empty]')) {
      const html = el.innerHTML;
      if (!html || !html.trim()) continue;
      const open = el.getAttribute('data-cve-open');
      if (!acc.has(open)) acc.set(open, { open, tag: el.tagName.toLowerCase(), html });
    }
  };
  grab();
  for (const d of document.querySelectorAll('details')) d.open = true;
  await new Promise((r) => setTimeout(r, 120));
  grab();
  // Computed BEFORE any click: clicking mutates aria-expanded/data-state, so
  // a live query would drift as we go.
  const triggers = [
    ...document.querySelectorAll('[aria-expanded="false"]'),
    ...document.querySelectorAll('[data-state="closed"] > *, [data-state="closed"]'),
  ].filter((e) => e.tagName === 'BUTTON' || e.tagName === 'SUMMARY' || e.getAttribute('role') === 'button');
  for (const t of triggers) {
    try { t.click(); } catch (e) {}
    await new Promise((r) => setTimeout(r, 120));
    grab();
  }
  await new Promise((r) => setTimeout(r, 200));
  grab();
  return [...acc.values()];
}"""


def open_tags_of_empty_elements(file_html):
    """Every `<tag …></tag>` in the file — the open tag, verbatim.

    Quote-aware: a Tailwind arbitrary variant puts '>' inside a class value
    (`[&[data-state=open]>svg]:rotate-90` — present on the very element this
    was written for), and a naive `[^>]*` truncates the tag there and matches
    nothing.
    """
    attrs = r"""(?:[^>"']|"[^"]*"|'[^']*')*"""
    out = {}
    for m in re.finditer(rf"<([a-z][a-z0-9-]*)\b{attrs}>\s*</\1>", file_html, re.I):
        whole = m.group(0)
        tag = m.group(1).lower()
        open_tag = whole[: whole.rfind("</")]
        # Only unambiguous ones: if the same opening tag occurs twice in the
        # file there is no way to say which is which, so skip rather than
        # write into the wrong place.
        if file_html.count(open_tag) == 1:
            out[open_tag] = tag
    return out


httpd, BASE = serve(DIST)
report = {"pages": {}, "scriptLiterals": [], "applied": bool(args.apply)}
filled_total = 0

# This script is idempotent by construction — an element that already has
# content is no longer "empty in the file" and is skipped — but its REPORT was
# not, and the report is now load-bearing: gate A2 reverses exactly the writes
# recorded here before comparing. So a second, no-op run used to overwrite a
# real record with an empty one, and the next gate A2 failed on the very
# transformation the record existed to account for. Prior writes are therefore
# carried forward whenever the element they describe is still filled on disk.
carried = {}
prev_path = Path(args.out)
if prev_path.exists():
    try:
        prev = json.loads(prev_path.read_text())
        if prev.get("applied"):
            for file, note in (prev.get("pages") or {}).items():
                kept = [d for d in (note.get("filled") or [])
                        if d.get("status") == "written" and d.get("openFull") and d.get("tag")]
                if kept:
                    carried[file] = {"file": file, "filled": kept, "fragment": note.get("fragment"),
                                     "carriedForward": True}
    except (OSError, ValueError):
        pass

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_context(viewport={"width": 1440, "height": 950}).new_page()

    for entry in MF["pages"]:
        f = entry["file"]
        src = DIST / f
        if not src.exists():
            continue
        file_html = src.read_text(errors="replace")
        # A carried-forward record is only honoured while its element is still
        # actually filled in the build — otherwise the record is stale and
        # exempting it in gate A2 would hide a genuine regression.
        if f in carried:
            still = [d for d in carried[f]["filled"]
                     if d["openFull"] in file_html
                     and not file_html.startswith(d["openFull"] + f"</{d['tag']}>",
                                                  file_html.index(d["openFull"]))]
            if still:
                report["pages"][f] = {**carried[f], "filled": still}
                report["applied"] = True
        empties = open_tags_of_empty_elements(file_html)
        if not empties:
            continue

        page.goto(f"{BASE}/{f}")
        page.wait_for_load_state("networkidle")
        # Mark the elements that were empty in the FILE, before expanding, so
        # the collection step compares like with like.
        page.evaluate(
            """(opens) => {
              const wanted = new Set(opens);
              for (const el of document.querySelectorAll('*')) {
                const open = el.outerHTML.slice(0, el.outerHTML.length - el.innerHTML.length - (el.tagName.length + 3));
                if (wanted.has(open)) {
                  el.setAttribute('data-cve-was-empty', '1');
                  el.setAttribute('data-cve-open', open);
                }
              }
            }""",
            list(empties.keys()),
        )
        found = page.evaluate(EXPAND_AND_COLLECT)
        if not found:
            continue

        # Write into the page's own source fragment, not into dist/ — dist is
        # a build artifact and the next `astro build` would throw this away.
        frag = FRAG / "bodies" / (re.sub(r"[^A-Za-z0-9]+", "-", f).strip("-") + ".html")
        if not frag.exists():
            cands = list((FRAG / "bodies").glob("*.html"))
            frag = next((c for c in cands if c.stem in f or f.replace("/", "-").startswith(c.stem)), None)
        page_note = {"file": f, "filled": [], "fragment": str(frag) if frag else None}

        if frag and frag.exists():
            body = frag.read_text()
            for item in found:
                open_tag = item["open"]
                needle = open_tag + f"</{item['tag']}>"
                if body.count(needle) != 1:
                    page_note["filled"].append({"open": open_tag[:70], "status": "not uniquely locatable in fragment"})
                    continue
                if args.apply:
                    body = body.replace(needle, open_tag + item["html"] + f"</{item['tag']}>", 1)
                page_note["filled"].append({
                    "open": open_tag[:70],
                    # The FULL opening tag and its tag name, untruncated — gate
                    # A2 needs them to reverse exactly this write and nothing
                    # else. Materializing is the pipeline's THIRD deliberate
                    # transformation (after path rooting and metadata
                    # position), and gate A2 knew only the first two, so any
                    # conversion that ran --apply failed a gate the skill says
                    # must pass with no exemptions. Recording the edit here is
                    # what lets that exemption be derived rather than declared.
                    "openFull": open_tag,
                    "tag": item["tag"],
                    "chars": len(item["html"]),
                    "text": re.sub(r"<[^>]+>", " ", item["html"])[:80].strip(),
                    "status": "written" if args.apply else "would write",
                })
                filled_total += 1
            if args.apply:
                frag.write_text(body)
        # Merge rather than replace: a page can have both writes carried
        # forward from an earlier apply and new ones found this run.
        if f in report["pages"]:
            report["pages"][f]["filled"].extend(page_note["filled"])
        else:
            report["pages"][f] = page_note

    browser.close()
httpd.shutdown()

# The script that filled those containers will fill them again on the next
# interaction — over whatever the owner has since written. Find the literal so
# it can go. Gate A afterwards is what proves removing it was safe: the words
# are in the markup now, so the page must render exactly as before.
texts = [d["text"] for pg in report["pages"].values() for d in pg["filled"] if d.get("text")]
for js in sorted(INPUT.rglob("*.js")):
    if any(part in (".audit", "node_modules") for part in js.parts):
        continue
    try:
        src_js = js.read_text(errors="replace")
    except OSError:
        continue
    for t in texts:
        probe = t[:40]
        if not probe or probe not in src_js:
            continue
        # The whole statement, so removal leaves valid JavaScript rather than
        # a dangling assignment target.
        at = src_js.index(probe)
        start = max(src_js.rfind(";", 0, at), src_js.rfind("{", 0, at), src_js.rfind("\n", 0, at)) + 1
        end = src_js.find(";", at)
        if end == -1:
            continue
        stmt = src_js[start:end + 1]
        # The text being in a file does NOT make the enclosing text a statement.
        # Copy very often lives in a DATA structure (a question→answer lookup,
        # an array of slides) that some assignment elsewhere consumes. The
        # boundary scan above then returns a fragment of that object literal —
        # verified live, `"…product support.",\n};` — and deleting it leaves the
        # object unclosed, so the whole file fails to parse and every unrelated
        # behaviour in it dies: theme toggle, mobile drawer, form handling. Gate
        # A does not catch that, because a page whose script never ran still
        # screenshots identically AT REST, which is the only state it captures.
        #
        # So removal is restricted to what the docstring actually means: an
        # assignment INTO the DOM. Anything else is reported with the sinks
        # that reference it and left alone — the operator wires it, which is
        # the pipeline's standing rule that ambiguity resolves to preservation
        # plus a logged decision.
        sink_re = re.compile(r"\.(?:innerHTML|textContent|innerText)\s*=|insertAdjacentHTML\s*\(")
        is_sink = bool(sink_re.search(stmt))
        if not is_sink:
            sinks = sorted({
                m.group(0).strip()
                for m in re.finditer(r"[^\n;{}]*(?:\.(?:innerHTML|textContent|innerText)\s*=|"
                                     r"insertAdjacentHTML\s*\()[^\n;]*", src_js)
            })
            report["scriptLiterals"].append({
                "file": str(js.relative_to(INPUT)),
                "statement": stmt.strip()[:160],
                "guarded": False,
                "kind": "data literal, not an assignment",
                "domSinksInFile": sinks[:6],
                "action": "LEFT IN PLACE — the copy lives in a data structure, so the text is not a "
                          "removable statement; removing the matched span would leave invalid "
                          "JavaScript. Remove the DOM assignment that consumes it by hand instead, "
                          "then re-run gate A and exercise the widget.",
            })
            continue
        # Even for a real sink, prove the removal leaves balanced JavaScript
        # before writing it. Cheap, and it is the difference between a script
        # that lost one line and a script that no longer parses.
        remainder = src_js[:start] + src_js[end + 1:]
        stripped = re.sub(r"(['\"`])(?:\\.|(?!\1).)*\1", "''", remainder, flags=re.S)
        stripped = re.sub(r"//[^\n]*|/\*.*?\*/", "", stripped, flags=re.S)
        balanced = all(stripped.count(a) == stripped.count(b) for a, b in ("{}", "()", "[]"))
        if not balanced:
            report["scriptLiterals"].append({
                "file": str(js.relative_to(INPUT)),
                "statement": stmt.strip()[:160],
                "guarded": False,
                "kind": "removal would unbalance the file",
                "action": "LEFT IN PLACE — deleting this span leaves unbalanced braces/parens, so "
                          "the file would stop parsing. Remove it by hand.",
            })
            continue
        # Some of these guard themselves — "fill this only if it is empty" is
        # how a careful author writes a runtime filler, and the reference site
        # did exactly that. Once the words are in the markup such a statement
        # can never fire again, so removing it changes nothing and risks
        # something. Left in place, and said so.
        guarded = bool(re.search(r"!\s*\w+(?:\.\w+)*\.(?:textContent|innerHTML|innerText)"
                                 r"(?:\.trim\(\))?\s*(?:&&|\))", stmt)
                       or re.search(r"(?:textContent|innerHTML|innerText)\s*===?\s*['\"]{2}", stmt))
        report["scriptLiterals"].append({
            "file": str(js.relative_to(INPUT)),
            "statement": stmt.strip()[:160],
            "guarded": guarded,
            "action": "left in place (guarded on empty — cannot overwrite once materialized)"
                      if guarded else ("removed" if args.apply else "to remove"),
        })
        if args.apply and not guarded:
            js.write_text(src_js[:start] + src_js[end + 1:])
            # The public/ copy the build serves has to lose it too.
            pub = WS / "astro-project" / "public" / js.relative_to(INPUT)
            if pub.exists():
                pub_src = pub.read_text(errors="replace")
                if stmt in pub_src:
                    pub.write_text(pub_src.replace(stmt, "", 1))

Path(args.out).write_text(json.dumps(report, indent=2))
verb = "materialized" if args.apply else "would materialize"
removable = [x for x in report["scriptLiterals"] if x["action"].startswith(("removed", "to remove"))]
left = [x for x in report["scriptLiterals"] if x not in removable]
print(f"{verb} {filled_total} JS-filled container(s) across {len(report['pages'])} page(s); "
      f"{len(removable)} script statement(s) {'removed' if args.apply else 'to remove'}, "
      f"{len(left)} LEFT IN PLACE → {args.out}")
for x in left:
    print(f"    {x['file']}: {x.get('kind', 'guarded')} — {x['action'].split(' — ')[0]}"
          + (f" (DOM sinks: {', '.join(x['domSinksInFile'])})" if x.get("domSinksInFile") else ""))
for pg, note in report["pages"].items():
    for d in note["filled"]:
        print(f"    {pg}: {d.get('status')} — \"{(d.get('text') or '')[:60]}…\"")
if args.apply:
    print("  REBUILD and re-run gate A: the words are in the markup now, so the page must render exactly as before.")
