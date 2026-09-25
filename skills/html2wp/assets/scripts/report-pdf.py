#!/usr/bin/env python3
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
"""The conversion report as a PDF: result.json's verdict and gates, then the report.

    report-pdf.py --result result.json --report CONVERSION-REPORT.md --out conversion-report.pdf

write-result.py runs this after it has written result.json. The page is built
here as HTML and printed by Playwright's Chromium with every network request
aborted, so the document cannot load anything — no font, no image, no URL a
report happened to quote. Carried over from the desktop app (runtime/report.py),
which printed the same kind of document for the owner.

Everything is escaped. The Markdown converter is deliberately small — headings,
paragraphs, `-` and numbered lists, `code`, fenced code, pipe tables and
**bold** — and passes no HTML through: a report quotes page copy and selectors,
and those are text. Exit 0 = the PDF was written; 1 = it could not be; 2 = usage.
"""
import argparse
import html
import json
import re
import sys
from pathlib import Path

STATUS = {"passed": "Passed", "failed": "Failed", "not_run": "Not run", "skipped": "Not applicable"}
ORDERED = re.compile(r"^\d+[.)]\s+")


def inline(text):
    """`code` spans verbatim, **bold**, everything else escaped."""
    out = []
    for part in re.split(r"(`[^`]+`)", text):
        if len(part) > 1 and part.startswith("`") and part.endswith("`"):
            out.append(f"<code>{html.escape(part[1:-1])}</code>")
        else:
            out.append(re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(part)))
    return "".join(out)


def cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def markdown(text):
    """The report's Markdown as HTML. A `#` heading becomes <h2> (the
    document's own title is the <h1>)."""
    lines, out, i = text.splitlines(), [], 0
    para, items, kind = [], [], None

    def flush():
        nonlocal para, items, kind
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
        if items:
            out.append(f"<{kind}>" + "".join(f"<li>{inline(x)}</li>" for x in items) + f"</{kind}>")
        para, items, kind = [], [], None

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
        elif m := re.match(r"^(#{1,6})\s+(.*)$", stripped):
            flush()
            level = min(len(m.group(1)) + 1, 6)
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
        elif stripped.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not re.fullmatch(r"[\s|:-]+", lines[i].strip()):
                    rows.append(cells(lines[i]))
                i += 1
            i -= 1
            if rows:
                head, body = rows[0], rows[1:]
                out.append("<table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                           + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in body)
                           + "</tbody></table>")
        elif re.match(r"^[-*]\s+", stripped) or ORDERED.match(stripped):
            want = "ol" if ORDERED.match(stripped) else "ul"
            if para or (kind and kind != want):
                flush()
            kind = want
            items.append(re.sub(r"^([-*]|\d+[.)])\s+", "", stripped))
        elif not stripped:
            flush()
        elif items and line[:1].isspace():
            items[-1] += " " + stripped
        else:
            if items:
                flush()
            para.append(stripped)
        i += 1
    flush()
    return "\n".join(out)


def document(result, report_md):
    e = lambda x: html.escape("" if x is None else str(x))
    site = result.get("site") or {}
    theme = result.get("theme") or {}
    mode = {"flash": "Flash", "astro": "Astro"}.get(result.get("mode"), "Full")
    rows = "".join(
        f"<tr><td>{e(g.get('id'))}</td><td>{e(g.get('stage'))}</td>"
        f"<td class=\"{e(g.get('status'))}\">{e(STATUS.get(g.get('status'), g.get('status')))}"
        f"{' — Flash: not repaired' if g.get('reportOnly') else ''}</td><td>{e(g.get('detail'))}</td></tr>"
        for g in result.get("gates") or [] if isinstance(g, dict))
    wired = result.get("wired") or {}
    blog, shop = wired.get("blog") or {}, wired.get("shop") or {}
    wired_line = (f"Menus: {e(wired.get('menusWired', 0))} of {e(wired.get('menus', 0))} wired · "
                  f"Blog: {'yes, ' + e(blog.get('posts', 0)) + ' post(s)' if blog.get('present') else 'no'} · "
                  f"Shop: {'yes, ' + e(shop.get('products', 0)) + ' product(s)' if shop.get('present') else 'no'} · "
                  f"Forms: {e(wired.get('forms', 0))} · Collections: {e(wired.get('collections', 0))}")
    repairs = [r for r in result.get("repairs") or [] if isinstance(r, dict)]
    outcome = {"fixed": "fixed", "failed": "still red", "open": "not closed"}
    repair_rows = "".join(
        f"<tr><td>{e(r.get('stage'))}</td><td>{e(r.get('attempt'))}/{e(r.get('of'))}"
        f"{' (the owner' + chr(39) + 's message)' if r.get('by') == 'owner' else ''}</td>"
        f"<td>{e(r.get('label') or r.get('lever'))}</td>"
        f"<td class=\"{'passed' if r.get('outcome') == 'fixed' else 'failed'}\">{e(outcome.get(r.get('outcome'), r.get('outcome')))}</td>"
        f"<td>{e(r.get('what'))}{(' — ' + e(r.get('note'))) if r.get('note') else ''}</td></tr>"
        for r in repairs)
    unfixed = "".join(
        f"<li>Stage {e(u.get('stage'))}: {e(u.get('what') or u.get('signature'))} — tried: "
        f"{e(', '.join(u.get('levers') or []) or 'no lever')}</li>"
        for u in result.get("couldNotFix") or [] if isinstance(u, dict))
    repairs_html = ((f"<h2>Repairs</h2><table><thead><tr><th>Stage</th><th>Attempt</th><th>Lever</th><th>Outcome</th>"
                     f"<th>The failure</th></tr></thead><tbody>{repair_rows}</tbody></table>") if repairs else "")
    if unfixed:
        repairs_html += f"<h2>What {e(mode)} could not fix</h2><ul>{unfixed}</ul>"
    stopped = result.get("stopped") or {}
    stop_line = (f"<p><strong>Stopped at stage {e(stopped.get('stage'))}:</strong> {e(stopped.get('reason'))}</p>"
                 if result.get("status") == "stopped" else "")
    return f"""<!doctype html><html lang="en"><meta charset="utf-8"><title>Conversion report</title><style>
@page{{size:A4;margin:18mm}}body{{font:11px/1.55 Arial,Helvetica,sans-serif;color:#222}}h1{{font-size:26px;margin:4px 0}}
h2{{font-size:16px;margin-top:22px}}h3{{font-size:13px;margin-top:16px}}h4,h5,h6{{font-size:12px}}
.meta{{color:#666}}header{{border-bottom:2px solid #222;padding-bottom:14px}}
table{{border-collapse:collapse;width:100%;font-size:10px;margin:8px 0}}td,th{{padding:6px;text-align:left;border-bottom:1px solid #ddd;vertical-align:top}}
tr{{break-inside:avoid}}td.failed{{color:#a40000;font-weight:bold}}td.passed{{color:#146c2e}}
code{{font:10px/1.4 Menlo,Consolas,monospace;background:#f3f3f3;padding:0 2px;overflow-wrap:anywhere}}
pre{{background:#f3f3f3;padding:8px;white-space:pre-wrap;overflow-wrap:anywhere}}pre code{{background:none}}
</style><header><div class="meta">html2wp · conversion report</div><h1>{e(site.get('name') or site.get('slug') or 'Site')}</h1>
<div class="meta">{e(mode)} conversion · {e(result.get('verdict'))} · {e(result.get('status'))} ·
{'Gutenberg block theme' if result.get('target') == 'gutenberg' else 'HTML WordPress theme'} · plugin {e(result.get('plugin'))}</div></header>
{stop_line}<h2>Theme</h2><p>Archive: {e(theme.get('file') or 'not packaged')}<br>SHA-256: <code>{e(theme.get('sha256') or 'not available')}</code></p>
<h2>Checks</h2><table><thead><tr><th>Check</th><th>Stage</th><th>Result</th><th>Details</th></tr></thead>
<tbody>{rows or '<tr><td colspan="4">No checks recorded.</td></tr>'}</tbody></table>
{repairs_html}<h2>What is wired</h2><p>{wired_line}</p>
<section style="break-before:page">{markdown(report_md)}</section></html>"""


def render(doc, out):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content(doc, wait_until="load")
            page.pdf(path=str(out), format="A4", print_background=True, display_header_footer=True,
                     header_template="<span></span>",
                     footer_template='<div style="font-size:8px;width:100%;text-align:center;color:#888">'
                                     'html2wp · <span class="pageNumber"></span> / <span class="totalPages"></span></div>')
        finally:
            browser.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--result", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    try:
        result = json.loads(Path(args.result).read_text())
        report_md = Path(args.report).read_text() if Path(args.report).is_file() else ""
    except (OSError, ValueError) as err:
        print(f"report-pdf: {err}", file=sys.stderr)
        return 2
    out = Path(args.out)
    tmp = out.with_name(f".{out.name}.tmp")
    try:
        render(document(result, report_md), tmp)
        tmp.replace(out)
    except Exception as err:  # noqa: BLE001 — a PDF that cannot be printed is reported, never a crash
        tmp.unlink(missing_ok=True)
        print(f"report-pdf: the PDF could not be rendered: {type(err).__name__}: {str(err).splitlines()[0][:200]}",
              file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
