#!/usr/bin/env python3
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
"""The article layout from the site's own article: parts/article.html.

    article-part.py {workspace} [--from <article page>] [--force] [--check]

Stage 3.5 in Flash, before make-zip, whenever the manifest has a blog. The
service derives the layout every post renders in from the first article page;
when it cannot (a region it reads as the site's chrome, no <main>, no
<article>) it ships a GENERIC layout whose classes this site does not define,
and make-zip refuses it — every post would render in a foreign skeleton. That
refusal used to stop the run with no theme.

This keeps a layout the service derived (its classes are the site's) and
otherwise derives one from the article page as the input has it
({workspace}/astro-project/dist, else static-src): the region
blog.articleMain names (default <main>), with the article's own fields as
[wp-article] tokens — the headline, its <time>, the image before the body,
the body itself, and a byline when every post carries an author — and a
<header>/<footer> inside it that repeats the site's own chrome left out (an
article's own <header>, the one holding its headline, stays). Paths are the
theme's: an image becomes __CLARA_THEME_URI__/<path>, a link to a page its
permalink. Anything else the article shows is baked in from that one article
and named in the report, because it would appear on every post as if true.

--from derives from another article page; --force replaces even a layout the
service derived; --check reports and writes nothing.

Writes parts/article.html and {workspace}/article-part.json (what was placed,
what was baked in). Exit 0 = kept or derived; 1 = could not derive (prints
`h2wp-signature: article-part-foreign`); 2 = usage.
"""
import argparse
import glob
import json
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from html_layout import VOID, parse_selector, select  # noqa: E402

SIGNATURE = "h2wp-signature: article-part-foreign"
DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2},? \d{4}|"
                  r"\d{1,2}\.? (jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{4})\b", re.I)
SPECIMEN = ('\n<div data-cve-specimen hidden aria-hidden="true">\n'
            '  <h2>Specimen heading</h2>\n'
            '  <p>Specimen paragraph with <a href="{blog}">a link</a>, <strong>bold</strong> and <em>italic</em>.</p>\n'
            '  <ul><li>Specimen list item</li></ul>\n'
            '  <blockquote><p>Specimen quotation.</p></blockquote>\n'
            '</div>\n')


class Tree(HTMLParser):
    """Elements with their source offsets: start (the `<`), open_end (after
    the start tag) and end (after the close tag, or the start tag's end for a
    void or unclosed element)."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.text = text
        self.lines = [0]
        for m in re.finditer("\n", text):
            self.lines.append(m.end())
        self.nodes, self.stack = [], []
        self.feed(text)
        self.close()
        for node in self.stack:
            node["end"] = len(text)

    def at(self):
        line, col = self.getpos()
        return self.lines[line - 1] + col

    def handle_starttag(self, tag, attrs):
        start = self.at()
        raw = self.get_starttag_text() or ""
        node = {"tag": tag, "attrs": {k: (v or "") for k, v in attrs}, "ancestors": list(self.stack),
                "start": start, "open_end": start + len(raw), "end": start + len(raw)}
        self.nodes.append(node)
        if tag not in VOID and not raw.endswith("/>"):
            self.stack.append(node)

    def handle_endtag(self, tag):
        start = self.at()
        close = self.text.find(">", start)
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                for node in self.stack[index:]:
                    node["end"] = close + 1 if node is self.stack[index] else start
                    node["close_start"] = start if node is self.stack[index] else start
                del self.stack[index:]
                break


def inside(node, outer):
    return outer["start"] <= node["start"] and node["end"] <= outer["end"] and node is not outer


def text_of(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def theme_css(theme):
    css = ""
    for f in glob.glob(os.path.join(theme, "**", "*.css"), recursive=True):
        try:
            css += Path(f).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            pass
    return css


def coverage(part, css):
    """make-zip's measure: the share of the part's class names the theme's CSS
    defines."""
    klass = set()
    for m in re.finditer(r'class="([^"]*)"', part):
        klass.update(c for c in m.group(1).split() if c)
    if not klass:
        return 1.0
    return round(sum(1 for c in klass if "." + c in css) / len(klass), 3)


def link_texts(html):
    return {text_of(m.group(1)).lower() for m in re.finditer(r"<a\b[^>]*>([\s\S]*?)</a>", html, re.I)} - {""}


def derive(html, page_file, manifest, theme, posts):
    """(part, placed, baked, why): the layout, the fields placed, the lines
    left baked in; why is set when nothing could be derived."""
    blog = manifest.get("blog") or {}
    nodes = Tree(html).nodes

    def pick(selector, within=None):
        steps = parse_selector(selector) if selector else None
        if not steps:
            return None
        found = [n for n in select(nodes, steps) if within is None or n is within or inside(n, within)]
        return found[0] if found else None

    region = pick(blog.get("articleMain") or "main")
    h1 = next((n for n in nodes if n["tag"] == "h1" and (region is None or inside(n, region))), None)
    if region is None:
        # No <main>: the smallest element holding the headline and the body.
        body0 = pick(blog.get("articleBody") or "article")
        anchor = body0 or h1
        if anchor is None:
            return None, [], [], "the article page has no <main>, no <article> and no <h1>"
        holds = lambda a: (h1 is None or h1 is a or inside(h1, a)) and (body0 is None or body0 is a or inside(body0, a))
        region = anchor
        for a in reversed(anchor["ancestors"]):
            if holds(region) or a["tag"] in ("body", "html"):
                break
            region = a
    if h1 is None:
        # The headline above the named region (a hero band over the body):
        # the region widens to the smallest box holding both, below <body>.
        top = next((n for n in nodes if n["tag"] == "h1"
                    and not any(a["tag"] in ("header", "footer", "nav") and not inside(region, a)
                                for a in n["ancestors"])), None)
        common = next((a for a in reversed(region["ancestors"]) if top is not None and inside(top, a)), None)
        if common is None or common["tag"] in ("body", "html"):
            return None, [], [], f"no <h1> inside {blog.get('articleMain') or 'main'} or around it"
        region, h1 = common, top

    # The site's own header and footer, when the region holds them (a <main>
    # that wraps the whole document): single.html renders them around the
    # part already. Told apart from the article's own <header> by what they
    # carry — the site's links — never by the tag alone; the one holding the
    # headline is the article's.
    chrome_links = set()
    for name in ("header", "footer"):
        for part_file in glob.glob(os.path.join(theme, "parts", f"{name}*.html")):
            try:
                chrome_links |= link_texts(Path(part_file).read_text(encoding="utf-8"))
            except OSError:
                pass
    cut = []
    for node in nodes:
        if not inside(node, region) or node["tag"] not in ("header", "footer", "nav") or inside(h1, node):
            continue
        if any(inside(node, c) for c in cut):
            continue
        links = link_texts(html[node["start"]:node["end"]])
        if links and chrome_links and len(links & chrome_links) / len(links) >= 0.6:
            cut.append(node)

    # The body: the article's own element, minus the head that holds the
    # headline (an <article><header><h1>…</header><div>…</div></article>).
    body = pick(blog.get("articleBody") or "article", region)
    content = None
    if body is not None:
        if inside(h1, body):
            head = next((c for c in [n for n in nodes if n["ancestors"] and n["ancestors"][-1] is body]
                         if c is h1 or inside(h1, c)), None)
            lead = [n for n in nodes if n["ancestors"] and n["ancestors"][-1] is body and head and n["start"] >= head["end"]]
            # A hero or a dateline straight after the headline belongs to the head.
            while lead and lead[0]["tag"] in ("figure", "img", "time", "picture") and not re.search(
                    r"<p\b", html[lead[0]["start"]:lead[0]["end"]]):
                head = lead.pop(0)
            content = (head["end"], body.get("close_start", body["end"])) if head else None
        else:
            content = (body["open_end"], body.get("close_start", body["end"]))
    if content is None:
        # No <article>: the element with the most paragraphs that is not the head.
        best, most = None, 1
        for node in nodes:
            if not inside(node, region) or inside(h1, node) or node is h1:
                continue
            paras = sum(1 for p in nodes if p["tag"] == "p" and p["ancestors"] and p["ancestors"][-1] is node)
            if paras > most and not inside(node, h1):
                best, most = node, paras
        if best is None:
            return None, [], [], "no body: no <article> and no element holding the article's paragraphs"
        content = (best["open_end"], best.get("close_start", best["end"]))
    in_body = lambda n: content[0] <= n["start"] and n["end"] <= content[1]

    edits, placed, baked = [], ["content"], []
    edits.append((content[0], content[0], '[wp-article field="content"]'))
    edits.append((content[1], content[1], "[/wp-article]"))
    edits.append((h1["open_end"], h1["open_end"], '[wp-article field="title"]'))
    edits.append((h1.get("close_start", h1["end"]), h1.get("close_start", h1["end"]), "[/wp-article]"))
    placed.append("title")
    time_el = next((n for n in nodes if n["tag"] == "time" and inside(n, region) and not in_body(n)
                    and not any(inside(n, c) for c in cut)), None)
    if time_el is not None and "close_start" in time_el:
        edits.append((time_el["open_end"], time_el["open_end"], '[wp-article field="date"]'))
        edits.append((time_el["close_start"], time_el["close_start"], "[/wp-article]"))
        placed.append("date")
    else:
        dated = next((text_of(html[n["open_end"]:n["close_start"]]) for n in nodes
                      if n["tag"] in ("p", "span", "div", "small", "li") and "close_start" in n and inside(n, region)
                      and not in_body(n) and not inside(h1, n) and n is not h1
                      and len(text_of(html[n["open_end"]:n["close_start"]])) <= 120
                      and DATE.search(text_of(html[n["open_end"]:n["close_start"]]))), None)
        baked.append(f"a date written as text, not a <time>: \"{dated}\" shows on every post" if dated else
                     "no <time> outside the body: every post shows the source article's date, if it shows one")
    hero = next((n for n in nodes if n["tag"] == "img" and inside(n, region) and not in_body(n)
                 and n["start"] < content[0] and not inside(n, h1) and not any(inside(n, c) for c in cut)), None)
    if hero is not None:
        edits.append((hero["start"], hero["start"], '[wp-article field="image"]'))
        edits.append((hero["end"], hero["end"], "[/wp-article]"))
        placed.append("image")
    authors = posts and all((p.get("author") or "").strip() for p in posts)
    byline = next((n for n in nodes if inside(n, region) and not in_body(n) and "close_start" in n
                   and (n["attrs"].get("rel") == "author" or re.search(r"(^|[\s_-])(author|byline)($|[\s_-])",
                                                                       n["attrs"].get("class", ""), re.I))
                   and not any(inside(n, c) for c in cut)
                   and not any(inside(x, n) for x in nodes if x["tag"] in ("img", "time", "a", "h1"))
                   and 0 < len(text_of(html[n["open_end"]:n["close_start"]])) <= 60), None)
    if byline is not None and authors:
        edits.append((byline["open_end"], byline["open_end"], '[wp-article field="author"]'))
        edits.append((byline["close_start"], byline["close_start"], "[/wp-article]"))
        placed.append("author")
    elif byline is not None:
        baked.append(f"the byline \"{text_of(html[byline['open_end']:byline['close_start']])}\" "
                     "(the posts carry no author)")
    for c in cut:
        edits.append((c["start"], c["end"], ""))

    # Offsets are the ORIGINAL page's: apply from the end, so each edit leaves
    # the ones before it where they were.
    edits.sort(key=lambda e: (e[0], e[1]), reverse=True)
    out = html
    for a, b, text in edits:
        out = out[:a] + text + out[b:]
    shift = sum(len(t) - (b - a) for a, b, t in edits if b <= region["start"])
    end_shift = sum(len(t) - (b - a) for a, b, t in edits if a < region["end"])
    part = out[region["start"] + shift: region["end"] + end_shift]
    if region["tag"] == "main":
        # WordPress draws its own <main> around the part: the region's stays
        # as a <div> carrying its classes.
        part = re.sub(r"^<main\b", "<div", part, count=1)
        part = re.sub(r"</main>\s*$", "</div>", part, count=1)
    return part, placed, baked, None


def page_links(manifest):
    out = {}
    for page in manifest.get("pages") or []:
        f, key = page.get("file") or "", page.get("key") or ""
        if f and key:
            out[f.lower()] = "/" if key == "front-page" or f == "index.html" else f"/{key}/"
    return out


def to_theme(part, page_file, manifest, theme, dist_root):
    """Relative asset paths → the theme's copy; links to pages → permalinks."""
    page_dir = os.path.dirname(page_file)
    links = page_links(manifest)
    missing = []

    def asset(path):
        if not path or re.match(r"^(https?:|//|data:|blob:|mailto:|tel:|#|\[|__CLARA_)", path) or "{" in path:
            return None
        clean = path.split("#")[0].split("?")[0]
        rel = os.path.normpath(clean.lstrip("/") if clean.startswith("/") else os.path.join(page_dir, clean))
        if rel.startswith(".."):
            return None
        if re.search(r"\.html?$", rel, re.I) or rel.endswith(os.sep) or rel == ".":
            return None
        if os.path.isfile(os.path.join(theme, rel)):
            return f"__CLARA_THEME_URI__/{rel}"
        missing.append(path)
        return None

    def link(href):
        if re.match(r"^(https?:|//|mailto:|tel:|#|\[|__CLARA_)", href or ""):
            return None
        clean, tail = re.match(r"^([^#?]*)(.*)$", href).groups()
        rel = os.path.normpath(clean.lstrip("/") if clean.startswith("/") else os.path.join(page_dir, clean or "."))
        for cand in (rel, rel + ".html", os.path.join(rel, "index.html")):
            if cand.lower() in links:
                return links[cand.lower()] + tail
        return None

    def attr(m):
        name, q, value = m.group(1), m.group(2), m.group(3)
        if name.lower() == "href":
            new = link(value) or asset(value)
        elif name.lower() == "srcset":
            cands = []
            for cand in value.split(","):
                bits = cand.strip().split()
                if bits:
                    bits[0] = asset(bits[0]) or bits[0]
                cands.append(" ".join(bits))
            new = ", ".join(cands)
        else:
            new = asset(value)
        return f" {name}={q}{new if new is not None else value}{q}"

    part = re.sub(r"\s(src|href|poster|srcset)=([\"'])([^\"']*)\2", attr, part, flags=re.I)
    part = re.sub(r"url\((['\"]?)([^)'\"]+)\1\)",
                  lambda m: f"url({m.group(1)}{asset(m.group(2)) or m.group(2)}{m.group(1)})", part)
    return part, sorted(set(missing))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("workspace")
    ap.add_argument("--from", dest="page", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    ws = Path(args.workspace).resolve()
    manifest = read_json(ws / "conversion-manifest.json")
    if not isinstance(manifest, dict):
        print(f"article-part: no conversion-manifest.json in {ws}", file=sys.stderr)
        return 2
    blog = manifest.get("blog") or {}
    report_path = ws / "article-part.json"
    if not blog.get("present"):
        print("article-part: no blog in the manifest — nothing to do")
        return 0
    slug = (manifest.get("site") or {}).get("slug") or ""
    theme = ws / "theme" / slug
    part_path = theme / "parts" / "article.html"
    if not slug or not part_path.parent.is_dir():
        print(f"article-part: no theme at {theme} (stage 3 builds it)", file=sys.stderr)
        return 2
    posts = read_json(theme / "clara-content" / "posts.json") or []
    css = theme_css(str(theme))
    current = part_path.read_text(encoding="utf-8") if part_path.is_file() else ""
    have = coverage(current, css) if current else 0.0
    service = (read_json(ws / "theme-report.json") or {}).get("articlePart") or {}
    if not args.force and current and have >= 0.5 and service.get("derived", True):
        report = {"kept": True, "coverage": have, "derivedFrom": service.get("derivedFrom")}
        if not args.check:
            report_path.write_text(json.dumps(report, indent=2) + "\n")
        print(f"article-part: kept the service's article layout ({int(have * 100)}% of its classes are the site's)")
        return 0

    page = args.page or (blog.get("articles") or [None])[0]
    if not page:
        print("article-part: the manifest's blog lists no article — nothing to derive from")
        return 0
    source, root = None, None
    for base in (ws / "astro-project" / "dist", ws / "static-src", ws / "input-untouched"):
        if (base / page).is_file():
            source, root = base / page, base
            break
    if source is None:
        print(f"article-part: {page} is in none of astro-project/dist, static-src, input-untouched", file=sys.stderr)
        print(SIGNATURE, file=sys.stderr)
        return 1
    html = source.read_text(encoding="utf-8", errors="replace")
    part, placed, baked, why = derive(html, page, manifest, str(theme), posts)
    if part is None:
        print(f"article-part: could not derive the article layout from {page}: {why}. Name the region "
              "(blog.articleMain / blog.articleBody) or derive from another article (--from).", file=sys.stderr)
        print(SIGNATURE, file=sys.stderr)
        return 1
    part, missing = to_theme(part, page, manifest, str(theme), str(root))
    blog_url = page_links(manifest).get((blog.get("listing") or "").lower(), "/")
    text = f"<!-- wp:html -->\n<!-- clara-ve-key: article -->\n{part.strip()}\n" + SPECIMEN.format(blog=blog_url) \
        + "<!-- /wp:html -->\n"
    share = coverage(text, css)
    report = {"kept": False, "derivedFrom": page, "placed": placed, "bakedIn": baked,
              "assetsNotInTheme": missing, "coverage": share,
              "replaced": {"derived": service.get("derived"), "coverage": have}}
    if share < 0.5:
        print(f"article-part: the layout derived from {page} uses classes this theme does not define "
              f"({int(share * 100)}%) — derive from another article (--from) or name the region.", file=sys.stderr)
        print(SIGNATURE, file=sys.stderr)
        if not args.check:
            report_path.write_text(json.dumps(report, indent=2) + "\n")
        return 1
    if not args.check:
        part_path.write_text(text, encoding="utf-8")
        report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"article-part: parts/article.html derived from {page} — fields: {', '.join(placed)}; "
          f"{int(share * 100)}% of its classes are the site's"
          + (f"; baked in from that one article: {'; '.join(baked)}" if baked else "")
          + (f"; not in the theme: {', '.join(missing[:5])}" if missing else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
