#!/usr/bin/env python3
"""Stage -2 — a live website becomes the local `dist/` this pipeline converts.

  python3 mirror-live.py --site https://example.com --out <mirror-dir>
      [--routes=/,/about,...] [--crawl] [--max-pages 40]
      [--threshold 0.006] [--no-verify] [--report mirror-report.json] [--force]

Every other input to this converter arrives as files on disk. An order that
arrives as a URL has no stage to enter at: `analyze-input.mjs` wants a
directory, and stage -1 wants a project it can build. This is that stage,
and its output is deliberately NOT the flat HTML stage 0 consumes — it is a
directory shaped like a build output, so the existing prerender runs on it
unchanged:

  python3 mirror-live.py  --site https://client.com --out mirror --routes=/,/about
  python3 prerender-spa.py --skip-build --dist mirror --out static-src \\
                           --project . --routes=/,/about

That composition is the whole point. Stage -1 already drives disclosure
controls, settles entrance motion and replays recorded behaviour from
markup; none of that cares whether the bytes it serves came from `npm run
build` or from someone else's server. Duplicating it here would mean
maintaining two copies of a gate-protected script.

WHY NOT wget / httrack / a "site downloader"
--------------------------------------------
A static mirror fetches what the HTML mentions. A modern site fetches what
its JavaScript decides to fetch: lazy `<img>` below the fold, a font named
only inside a stylesheet, a route chunk pulled on hover, a Lottie JSON
loaded at runtime. None of those appear in the markup a plain GET returns.
So this script does not parse HTML for asset references — it runs the page
in Chromium and writes down every response the BROWSER actually received.
What the browser fetched is, by construction, what the page needs.

Single-file archivers (monolith, SingleFile) fail the opposite way: they
inline everything into one document, and the bundler downstream reads real
`url()` and `srcset` out of real stylesheet files.

THE GATE
--------
This stage owns a seam that is invisible from every later gate, and the
reason is worth stating plainly. Once an asset is missing from the mirror,
prerender serves the mirror locally and captures from it — so the asset is
missing on BOTH sides of gate -1, which compares dist against capture and
passes green. Gate A then compares two copies of the same hole. The only
place mirror completeness can be observed at all is live-vs-mirror, which
is what gate -2 does: full-page screenshots of the real site against the
locally served mirror, at 1440/820/390, per route, at gate -1's threshold.

The mirror side runs with every non-localhost request ABORTED. Without that
an absolute URL left un-rewritten would quietly fetch from the live origin
during the comparison, and the gate would certify a mirror that only works
while the client's server is up.

Exit 0 = every route within threshold; 1 = a route drifted or an asset was
unreachable; 2 = refused (site unreachable, no routes, bad arguments).
"""

import argparse, functools, glob, hashlib, ipaddress, json, mimetypes, os, re, shutil, socket, sys, threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from net_guard import (  # noqa: E402
    address_verdict, attach_network_guard, guarded_api_get, is_private_url,
)

from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--site", default="", help="origin to mirror, e.g. https://example.com")
ap.add_argument("--serve-only", action="store_true",
                help="serve an existing --out (with its header sidecars) and do nothing else")
ap.add_argument("--out", required=True, help="directory to write the dist-shaped mirror into")
ap.add_argument("--routes", default="", help="comma-separated route paths; default '/' (plus --crawl)")
ap.add_argument("--crawl", action="store_true",
                help="follow same-origin links instead of mirroring only --routes")
ap.add_argument("--max-pages", type=int, default=40, help="crawl ceiling (default 40)")
ap.add_argument("--threshold", type=float, default=0.006, help="same 0.6%% as gate -1 and gate A")
ap.add_argument("--no-verify", action="store_true", help="skip gate -2 (never on a real conversion)")
ap.add_argument("--report", default="", help="default: mirror-report.json beside --out")
ap.add_argument("--force", action="store_true", help="clear --out even without this script's marker")
ap.add_argument("--timeout", type=int, default=45000, help="per-navigation timeout in ms")
# The mirror is only viewable over HTTP — its references are root-relative,
# so opening index.html from the filesystem asks for `/assets/...` at the
# root of the DISK and the page arrives with no CSS, no images, no scripts,
# looking like a failed scrape. Serving it is therefore the default, not a
# convenience: the alternative is a correct mirror that reads as broken.
ap.add_argument("--no-serve", action="store_true",
                help="do not serve the finished mirror (use when chaining into stage -1)")
ap.add_argument("--port", type=int, default=8000, help="port for the final serve (default 8000)")
args = ap.parse_args()

SITE = args.site.rstrip("/")
SITE_HOST = urlparse(SITE).netloc
if not SITE_HOST and not args.serve_only:
    print(f"--site must be an absolute URL, got {args.site!r}", file=sys.stderr)
    sys.exit(2)


# TLS verification, unless the operator turns it off on purpose.
#
# Every browser context here used to pass True unconditionally. The one that
# matters is gate -2, which compares the LIVE client site against the
# mirror: the fidelity verdict this whole stage exists to produce was being
# taken over an unauthenticated channel. The flag was there for sites with a
# broken or self-signed certificate, which is a real case and now an explicit
# one.
IGNORE_TLS = os.environ.get("H2WP_INSECURE_TLS") == "1"
if IGNORE_TLS:
    print("warning: TLS certificate errors are being ignored (H2WP_INSECURE_TLS=1)", file=sys.stderr)


# One switch for both the --site check and the per-request one, so a person
# who deliberately mirrors something on their own network is not asked twice.
IGNORE_PRIVATE = os.environ.get("H2WP_ALLOW_PRIVATE_MIRROR") == "1"


# Every subrequest the mirrored page makes that pointed somewhere private.
# Reported, not silently dropped: a page reaching for 169.254.169.254 is worth
# the owner knowing about whether or not it succeeded.
private_subrequests = []


def request_is_private(url):
    """Should this request be refused? Called from the browser route handler.

    Checking only --site was not enough, and this is the half that was
    missing. The address of the PAGE says nothing about where its scripts,
    images, iframes and fetches point: a page served from a public host can
    ask the browser for http://169.254.169.254/latest/meta-data/ or
    http://192.168.1.1/, and the mirror would have fetched it, written it to
    disk and shipped it in the conversion. The route handler saw a GET and
    waved it through.
    """
    if IGNORE_PRIVATE:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None  # data:, blob:, about: — nothing is fetched over the network
    host = parsed.hostname
    if not host:
        return None
    return is_private_url(url)


def refuse_private(url, what="--site"):
    """Stop, unless the operator has said this is deliberate."""
    reason = address_verdict(url)
    if reason is None:
        return
    if IGNORE_PRIVATE:
        print(f"warning: {what} {url} — {reason} (allowed by H2WP_ALLOW_PRIVATE_MIRROR)", file=sys.stderr)
        return
    print(f"refusing {what}: {reason}.", file=sys.stderr)
    print("  A mirror fetches whatever this points at and puts it in the output.", file=sys.stderr)
    print("  If you meant a machine on your own network, set H2WP_ALLOW_PRIVATE_MIRROR=1.", file=sys.stderr)
    sys.exit(2)


if not args.serve_only:
    refuse_private(SITE)

OUT = Path(args.out).resolve()
REPORT = Path(args.report).resolve() if args.report else OUT.parent / "mirror-report.json"
MARKER = ".mirror-live"
WIDTHS = [("desktop", 1440), ("tablet", 820), ("mobile", 390)]

report = {
    "site": SITE, "out": str(OUT), "routes": [], "pages": {},
    "assets": 0, "externalHosts": [], "unlistedRoutes": [],
    "unreachable": [], "dynamicEndpoints": [], "warnings": [], "passed": True,
}

# url-without-query -> path inside the mirror, root-relative with a leading /
captured = {}
# mirror path -> sha1 of the bytes written there, so a collision is noticed
written = {}
# every non-GET this script refused to send to the client's live server
blocked_writes = []


def warn(msg):
    report["warnings"].append(msg)
    print(f"  warn: {msg}")


# ---------------------------------------------------------------- paths

def safe_urlparse(url):
    """`urlparse` raises on a malformed authority — an unbalanced `[` makes it
    cry "Invalid IPv6 URL". Everything this script parses out of a REGEX match
    is untrusted by construction: a minified bundle contains string fragments
    like `http://` + expr, and the pattern cannot tell those from addresses.
    A fragment must be skipped, never crash a mirror that is otherwise done —
    it did exactly that on produkcnatlac.sk, after 146 files were captured."""
    try:
        return urlparse(url)
    except ValueError:
        return None


def canon(url):
    """The capture key: everything that identifies the BYTES, fragment aside.

    The query string belongs in the key. `/_image?w=256&q=10` and
    `/_image?w=1600` are a blurred placeholder and the real hero image;
    keying on the path alone makes them the same asset, keeps whichever
    arrived first, and puts the placeholder on the page."""
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, p.query, ""))


# Chromium serves these routinely; Python 3.9's mimetypes does not know them.
EXT_BY_TYPE = {
    "image/webp": ".webp", "image/avif": ".avif", "image/svg+xml": ".svg",
    "font/woff2": ".woff2", "font/woff": ".woff", "font/ttf": ".ttf",
    "text/css": ".css", "application/javascript": ".js", "text/javascript": ".js",
    "application/json": ".json", "text/html": ".html",
}


def guess_ext(content_type):
    base = (content_type or "").split(";")[0].strip().lower()
    return EXT_BY_TYPE.get(base) or (mimetypes.guess_extension(base) if base else "") or ""


def url_to_mirror_path(url, is_document, content_type=None):
    """Absolute URL -> root-relative path inside the mirror.

    Routes are written directory-style (`about/index.html`, not
    `about.html`) for one reason that has already cost this pipeline a
    conversion once: the stock server resolves `/about` to a directory
    index on its own, so the mirror never needs an SPA fallback. A mirror
    served WITH a fallback answers every extensionless route with the front
    page, and each route is then captured as a copy of the home page —
    identically on both sides of every downstream gate."""
    p = urlparse(url)
    # DECODED, because the file has to be findable by the server: a request
    # for `/fonts/Kunst%20Grotesk.woff2` is unquoted before the filesystem is
    # touched, so a file literally named `Kunst%20Grotesk.woff2` is a 404.
    # Measured on cloudflare.com: three webfaces 404'd that way, Chromium
    # fell back to another face of the same family, its wider glyphs pushed
    # one testimonial into an extra line, and gate -2 read the 19px shove
    # through everything below it as a 6.25% failure. Any client asset named
    # "Logo Final.png" or "kaviareň.jpg" fails identically.
    path = unquote(p.path or "/")
    prefix = "" if p.netloc == SITE_HOST else f"/_ext/{p.netloc}"
    if is_document:
        if path.endswith("/"):
            path += "index.html"
        elif not Path(path).suffix:
            path += "/index.html"
        # A document at an asset-looking address (`/feed.xml`, `/page.php`)
        # keeps its address — the extension is the client's, not ours.
    elif path.endswith("/"):
        # An endpoint addressed as a directory (`https://cdn.example/?id=1`).
        # Writing that verbatim tries to open a directory as a file.
        path += "index"
    # Give it an extension BEFORE the query hash, so the hash is never
    # mistaken for one — `/_image.a1b2c3d4` would otherwise look extensioned.
    if not Path(path).suffix:
        ext = guess_ext(content_type)
        if ext:
            path += ext
        else:
            warn(f"{url} has neither a file extension nor a usable content type")
    if p.query:
        stem = Path(path)
        path = f"{stem.with_suffix('')}.{hashlib.sha1(p.query.encode()).hexdigest()[:8]}{stem.suffix}"
    return re.sub(r"/{2,}", "/", prefix + path)


def ref_path(path):
    """The disk path re-encoded for use INSIDE a document.

    Disk and reference disagree on purpose: the file is `Kunst Grotesk.woff2`
    so the server can find it, and the CSS must say
    `Kunst%20Grotesk.woff2`, because a raw space inside `url()` does not
    survive a stylesheet parser."""
    return quote(path, safe="/")


def link_target(path):
    """Where the bytes live, versus what a LINK to them must say.

    `/about/index.html` is the file; `/about/` is the href. The difference
    matters because that string does not stop at the mirror — stage -1
    captures it, the theme carries it, and it is delivered as the address a
    visitor clicks on the finished WordPress site."""
    return path[: -len("index.html")] if path.endswith("/index.html") else path


def document_aliases(key, is_document):
    """`/about` and `/about/` address one document. Which form the capture is
    keyed under is decided by whether the server redirects; the markup
    routinely says the other one. An internal link that fails to match is
    left absolute, and the delivered site then links back to the client's
    live domain — silently, because a link target is never fetched, so
    gate -2 cannot see it and no later gate compares link targets at all."""
    if not is_document:
        return []
    p = urlparse(key)
    if p.path in ("", "/"):
        return []
    other = p.path.rstrip("/") if p.path.endswith("/") else p.path + "/"
    return [urlunparse((p.scheme, p.netloc, other, "", p.query, ""))]


def safe_local(mirror_path):
    """Mirror path -> file on disk, refusing anything that climbs out of OUT."""
    rel = mirror_path.lstrip("/")
    dest = (OUT / rel).resolve()
    if OUT not in dest.parents and dest != OUT:
        return None
    return dest


def write_asset(mirror_path, body):
    dest = safe_local(mirror_path)
    if dest is None:
        warn(f"refused to write outside the mirror: {mirror_path}")
        return
    digest = hashlib.sha1(body).hexdigest()
    if mirror_path in written:
        if written[mirror_path] != digest:
            # Two URLs that differ only by query string, serving different
            # bytes. Reported rather than silently keeping the first.
            warn(f"{mirror_path} was written twice with different content — kept the first")
        return
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
    except OSError as e:
        # A file and a directory claiming one name (`/api/x` the endpoint,
        # `/api/x/posts` its sibling) cannot both exist on disk. One loud
        # line beats an unfinished mirror.
        warn(f"could not write {mirror_path}: {e}")
        return
    written[mirror_path] = digest


# ---------------------------------------------------------------- capture

def is_html(content_type):
    return "text/html" in (content_type or "").lower()


def is_css(content_type, url):
    return "text/css" in (content_type or "").lower() or url.split("?")[0].endswith(".css")


def attach_capture(ctx):
    """Write down every response the browser received.

    Bodies are stored VERBATIM — the document response here is pre-JS HTML,
    which is exactly what makes the mirror a real build output that stage -1
    can drive. Capturing `outerHTML` instead would hand prerender a page
    whose framework had already run, and stage -1's whole contract is that it
    drives the framework itself."""
    def on_response(resp):
        url = resp.url
        if not url.startswith(("http://", "https://")):
            return
        if 300 <= resp.status < 400:
            return
        if resp.status >= 400:
            entry = {"url": url, "status": resp.status}
            if entry not in report["unreachable"]:
                report["unreachable"].append(entry)
            return
        key = canon(url)
        if key in captured:
            return
        host = urlparse(url).netloc
        try:
            if host != SITE_HOST and resp.frame.parent_frame is not None:
                # A cross-origin subframe is somebody else's page — a YouTube
                # player, a Maps tile server. Its innards are not assets of
                # the site being converted, and the embed is supposed to keep
                # loading from its own origin on the delivered site.
                return
        except Exception:
            pass
        try:
            body = resp.body()
        except Exception:
            # Aborted, streamed or already-discarded responses have no body
            # to read. Nothing is faked in its place.
            return
        ctype = resp.header_value("content-type")
        path = url_to_mirror_path(url, is_html(ctype), ctype)
        write_asset(path, body)
        captured[key] = link_target(ref_path(path))
        for alias in document_aliases(key, is_html(ctype)):
            captured.setdefault(alias, captured[key])
        # A same-origin runtime endpoint answers at its EXACT address or not
        # at all. The extension appended above serves everything the REWRITER
        # points at it — but a URL the page's own code assembles at runtime
        # is never rewritten, and the server unquotes-and-looks-up literally.
        # Measured on produkcnatlac.sk: the blog listing calls
        # `/_serverFn/<hash>`, the capture existed only as `<hash>.json`, and
        # the whole listing rendered as an error page off the 404. So the
        # bytes are written twice, once under each name. Only query-less URLs
        # get this: forty `?payload=` variants of one path cannot share one
        # file, and serving variant one to every caller would be WRONG data
        # rather than an honest 404 — those are reported instead.
        u = safe_urlparse(url)
        if (u and u.netloc == SITE_HOST and not u.query
                and not is_html(ctype) and not Path(unquote(u.path)).suffix):
            exact = re.sub(r"/{2,}", "/", unquote(u.path))
            if exact != path:
                write_asset(exact, body)
                # The body alone is not the response. produkcnatlac's blog
                # client checks `x-tss-serialized` BEFORE deserialising —
                # correct bytes with the wrong headers still render the error
                # page, measured twice (octet-stream, then even with a proper
                # content-type). So the headers that shaped the reply travel
                # in a sidecar, and serve() replays them.
                try:
                    keep = {k: v for k, v in resp.headers.items()
                            if k == "content-type" or k.startswith("x-")}
                except Exception:
                    keep = {"content-type": ctype} if ctype else {}
                if keep:
                    side = safe_local(exact + HEADER_SIDECAR)
                    if side is not None:
                        side.write_text(json.dumps(keep, indent=1))
        elif u and u.netloc == SITE_HOST and u.query and not Path(unquote(u.path)).suffix:
            # A parameterised endpoint IS replayable — for the questions that
            # were actually asked. Each variant already sits under its
            # query-hash name; serve() re-derives the same hash from a live
            # request's query and finds it, headers riding in a sidecar per
            # variant. An unseen payload still 404s, which is honest — the
            # alternative was serving somebody else's answer.
            try:
                keep = {k: v for k, v in resp.headers.items()
                        if k == "content-type" or k.startswith("x-")}
            except Exception:
                keep = {"content-type": ctype} if ctype else {}
            if keep:
                side = safe_local(path + HEADER_SIDECAR)
                if side is not None and not side.exists():
                    side.parent.mkdir(parents=True, exist_ok=True)
                    side.write_text(json.dumps(keep, indent=1))
            q = unquote(u.path)
            if q not in report["dynamicEndpoints"]:
                report["dynamicEndpoints"].append(q)
        if host != SITE_HOST and host not in report["externalHosts"]:
            report["externalHosts"].append(host)

    ctx.on("response", on_response)


SETTLE_JS = """async () => {
  document.documentElement.style.scrollBehavior = 'auto';
  for (const i of document.querySelectorAll('img[loading=lazy]')) i.loading = 'eager';
  const h = document.body.scrollHeight;
  for (let y = 0; y < h; y += 600) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 110)); }
  window.scrollTo(0, 0);
}"""

# Only the network side effects of a disclosure matter here — the DOM deltas
# are stage -1's job and it records them properly, by identity and per width.
# This pass exists so the panel that fetches an image WHEN OPENED has that
# image in the mirror by the time stage -1 goes looking for it.
CLICK_JS = """async () => {
  const sel = 'button, summary, [aria-expanded], [role=tab], [aria-controls], [data-toggle], [data-tab]';
  // A <button> inside a <form> submits unless it says otherwise — `type` is
  // OPTIONAL and defaults to "submit", so the dangerous case is the one
  // nobody wrote an attribute for. Clicking it sends the client's contact
  // form for real. Measured on produkcnatlac.sk, where this pass fired a
  // POST to /api/public/inquiries; empty fields failed validation that time,
  // which is luck, not a safeguard.
  const submits = el => {
    if (el.tagName !== 'BUTTON' || !el.closest('form')) return false;
    return (el.getAttribute('type') || 'submit').toLowerCase() !== 'button';
  };
  const els = [...document.querySelectorAll(sel)].slice(0, 120);
  let clicked = 0, skipped = 0;
  for (const el of els) {
    if (submits(el)) { skipped++; continue; }
    try { el.click(); clicked++; } catch (e) {}
    await new Promise(r => setTimeout(r, 110));
  }
  return { clicked, skipped };
}"""

LINKS_JS = """() => [...document.querySelectorAll('a[href]')].map(a => a.href)"""

MEDIA_JS = """() => {
  const out = [];
  for (const v of document.querySelectorAll('video, audio')) {
    if (v.currentSrc || v.src) out.push(v.currentSrc || v.src);
    for (const s of v.querySelectorAll('source')) if (s.src) out.push(s.src);
  }
  return out;
}"""


# Stability is MEASURED, not assumed. Sample the inline styles plus the
# computed transform of everything animating, and wait until the sample stops
# changing — the same discipline as stage -1's settle(), and for the same
# reason: a screenshot taken mid-animation compares an intermediate state
# against a different intermediate state. Observed on cloudflare.com, whose
# quote block repaints its text orange as the page scrolls: the two sides
# were photographed at different points of that sweep and gate -2 read the
# identical, fully-present text as a 6.29% failure.
MOTION_SIG_JS = """() => {
  let s = '';
  for (const el of document.querySelectorAll('[style]')) {
    s += el.getAttribute('style') + '|' + getComputedStyle(el).transform + ';';
  }
  for (const a of document.getAnimations()) {
    try {
      const t = a.effect && a.effect.target;
      if (t) s += getComputedStyle(t).transform + ',' + getComputedStyle(t).opacity + ';';
    } catch (e) {}
  }
  return s;
}"""


def settle(page, quick=False):
    """`quick` is for the mirroring pass, which only needs the network side
    effects of a scroll-through; the gate needs a page that has stopped
    moving, and pays for it."""
    try:
        page.wait_for_load_state("networkidle", timeout=args.timeout)
    except Exception:
        warn(f"{page.url} never reached network idle — mirroring what arrived")
    try:
        page.evaluate("document.fonts && document.fonts.ready")
        page.evaluate(SETTLE_JS)
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    if quick:
        return
    last, stable, waited = None, 0, 0
    while waited < 26000:
        try:
            sig = page.evaluate(MOTION_SIG_JS)
        except Exception:
            break
        stable = stable + 1 if sig == last else 0
        last = sig
        if stable >= 3:
            break
        page.wait_for_timeout(300)
        waited += 300
    else:
        warn(f"{page.url} never stopped animating within 26s — the shot may hold a mid-animation frame")
    # Bytes arrived is not raster exists: a full-page shot paints far outside
    # the viewport and Chromium decodes lazily.
    waited = 0
    while waited < 15000:
        try:
            pending = page.evaluate(
                "() => [...document.querySelectorAll('img')].filter(i => !i.complete).length")
        except Exception:
            break
        if not pending:
            break
        page.wait_for_timeout(250)
        waited += 250
    try:
        page.evaluate(
            "async () => { await Promise.all([...document.querySelectorAll('img')]"
            ".map(i => i.decode().catch(() => {}))); }")
    except Exception:
        pass


def mirror_route(browser, route, discovered):
    """Load one route at both widths, trigger what the page fetches lazily,
    and record the links it offers. Both widths are visited because a
    responsive site ships different images to each — a `srcset` the desktop
    never requests is still an asset the mobile layout needs."""
    url = SITE + route
    page_report = report["pages"].setdefault(route, {})
    for label, w in (("mobile", 390), ("desktop", 1440)):
        ctx = browser.new_context(viewport={"width": w, "height": 900},
                                  service_workers="block", ignore_https_errors=IGNORE_TLS)
        def capture_block(request, reason):
            if reason.startswith("method "):
                blocked_writes.append(f"{request.method} {request.url}")
            else:
                private_subrequests.append(f"{request.url} ({reason})")

        # Installed before page creation/navigation. Address checks run before
        # the read-only method rule for every document, redirect and
        # subrequest the live page can initiate.
        attach_network_guard(
            ctx, checker=request_is_private, allowed_methods=("GET",),
            on_block=capture_block,
        )
        attach_capture(ctx)
        page = ctx.new_page()
        try:
            resp = page.goto(url, wait_until="commit", timeout=args.timeout)
        except Exception as e:
            warn(f"{url} @{label} did not load: {str(e).splitlines()[0]}")
            ctx.close()
            continue
        if resp is not None and resp.status >= 400:
            warn(f"{url} answered {resp.status} — route not mirrored")
            page_report["status"] = resp.status
            ctx.close()
            continue
        settle(page, quick=True)
        try:
            page_report["clicks"] = page.evaluate(CLICK_JS)
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            # A click navigated away or detached the page. The responses it
            # produced are already captured; the route itself is reloaded by
            # the next width, or was already captured by this one.
            pass
        try:
            for href in page.evaluate(LINKS_JS):
                if urlparse(href).netloc != SITE_HOST:
                    continue
                r = urlparse(href).path or "/"
                if not Path(r).suffix or r.endswith((".html", ".htm")):
                    discovered.add(r)
            for m in page.evaluate(MEDIA_JS):
                if strip_query(m) not in captured and m.startswith("http"):
                    warn(f"media never fetched by the browser, absent from the mirror: {m}")
        except Exception:
            pass
        ctx.close()
    return page_report


# ---------------------------------------------------------------- rewriting

# Absolute, protocol-relative, and root-relative-WITH-QUERY. The last form
# is not optional: once a query string is part of the mirrored filename, a
# `srcset="/_image?w=600 600w"` left untouched asks the local server for a
# `/_image` that no longer exists.
URL_RE = re.compile(
    r"""(?<![\w.-])(?:(?:https?:)?//[^\s"'(),<>\\]+|/[^\s"'(),<>\\]*\?[^\s"'(),<>\\]*)"""
)

TAG_RE = re.compile(r"<\s*([a-zA-Z][\w-]*)[^<>]*$", re.S)
REMOTE_ATTR = {"a": "href", "area": "href", "form": "action",
               "iframe": "src", "embed": "src", "object": "data"}
# Namespace declarations are identifiers that merely look like addresses.
NON_ASSET_HOSTS = {"www.w3.org", "w3.org"}


def stays_remote(text, at):
    """Is the CROSS-ORIGIN URL at this offset an address that must keep
    pointing where it points, rather than a file to mirror?

    Two kinds live here. An outbound `<a href>` or a third-party `<form
    action>` is somewhere the visitor GOES — a POST endpoint is not a file,
    and reporting social links as missing assets buries the real findings
    under a list nobody reads. And a cross-origin `<iframe>` is an embed:
    a YouTube player or a Maps frame is SUPPOSED to load from its own
    origin, so rewriting its `src` to a mirrored copy ships a dead player
    into the client's WordPress site.

    `<link href>` is the counter-example that fixes the rule: same attribute
    as an anchor, and it IS an asset. The tag decides, never the attribute.

    Same-origin URLs never reach this test — an internal link must be
    rewritten, or the delivered site points back at the old domain."""
    head = text[max(0, at - 300):at]
    m = TAG_RE.search(head)
    if not m:
        return False
    attr = REMOTE_ATTR.get(m.group(1).lower())
    return bool(attr) and bool(re.search(attr + r"""\s*=\s*["']?[^"']*$""", head))


def rewrite_text(text, where, warn_misses=True):
    """Point every absolute URL we captured at the mirror copy.

    Root-relative is the target form (`/assets/app.css`), not a `../` chain:
    the mirror is always SERVED over HTTP, by this script's gate and by
    stage -1 alike, so a root-relative path resolves from any depth without
    the rewriter having to know how deep the referring document sits."""
    misses = []
    is_markup = where.lower().endswith((".html", ".htm", ".svg", ".xml"))

    def sub(m):
        raw = m.group(0)
        # A page that DISPLAYS markup (a docs demo, a code sample) contains
        # escaped tags, and the regex happily runs a URL into the `&quot;`
        # that ends it. Only these three end a URL — `&amp;` is legitimately
        # inside one.
        tail = ""
        cuts = [raw.find(e) for e in ("&quot;", "&gt;", "&lt;") if raw.find(e) != -1]
        if cuts:
            raw, tail = raw[:min(cuts)], raw[min(cuts):]
        if raw.startswith("//"):
            absolute = "https:" + raw
        elif raw.startswith("/"):
            absolute = SITE + raw
        else:
            absolute = raw
        parsed = safe_urlparse(absolute)
        if parsed is None:
            return raw + tail
        host = parsed.netloc
        cross = bool(host) and host != SITE_HOST
        # Tested BEFORE the capture lookup, not after: an embedded YouTube
        # player IS captured (the browser fetched it), and rewriting a hit
        # is exactly the mistake — the embed must survive as a remote
        # address, not become a local copy.
        if cross and is_markup and stays_remote(text, m.start()):
            return raw + tail
        # `&amp;` is how the browser was told the URL, not how it fetched it.
        key = canon(absolute.replace("&amp;", "&"))
        if key in captured:
            return captured[key] + tail
        if cross and host not in NON_ASSET_HOSTS:
            misses.append(absolute)
        return raw + tail

    out = URL_RE.sub(sub, text)
    if warn_misses:
        for u in dict.fromkeys(misses):
            warn(f"{where} still points at {u} — not captured, so it loads from the network")
    return out


def rewrite_pass():
    """Second pass, once every route has been visited: an asset referenced by
    the front page may only have been fetched while mirroring the last one,
    and rewriting as we went would have missed it."""
    n = 0
    for f in sorted(OUT.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in (".html", ".htm", ".css", ".js", ".json", ".xml", ".svg"):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = "/" + str(f.relative_to(OUT))
        # A script's own string literals are not rewritten — a URL built by
        # concatenation cannot be found by a regex, and a partial rewrite of
        # a URL the code assembles produces a broken address rather than an
        # honest miss. JS is scanned only so the misses get REPORTED.
        if f.suffix.lower() == ".js":
            rewrite_text(text, rel)
            continue
        # A feed carries the site's own writing, with the article HTML escaped
        # inside it — so the tag test cannot see tags, and every outbound link
        # an author ever wrote is reported as a missing asset. Rewrite it,
        # but stay quiet about what it links to.
        new = rewrite_text(text, rel, warn_misses=f.suffix.lower() != ".xml")
        if new != text:
            f.write_text(new, encoding="utf-8")
            n += 1
    return n


# ------------------------------------------------------- referenced sweep

# `content=` needs a stricter shape than the rest. Every `<meta>` description
# and `og:title` on the site flows through this attribute as PROSE, and a
# title that happens to end in a file-looking word ("Simple.css") is then
# fetched as an asset and 404s — a finding invented by the scanner, on a
# mirror that is correct. So `content=` is read only when the value is
# shaped like a path; `src`/`href` still accept a bare relative name.
REF_RE = re.compile(
    r"""(?:src|href|poster|data-src)\s*=\s*["']([^"']+)["']"""
    r"""|content\s*=\s*["']((?:https?:)?/[^"']+|\.{1,2}/[^"']+)["']"""
    r"""|url\(\s*["']?([^"')]+)""",
    re.I,
)
SWEEP_CEILING = 200


def sweep_referenced(pw):
    """Fetch same-origin assets the markup NAMES but the browser never asked for.

    A favicon, an `og:image`, a `<video>` source behind a play button: each
    is named in the delivered markup, none is requested during a headless
    render, and none is visible to gate -2 — a screenshot does not show a
    favicon, so the mirror scores 0.00% while the asset is simply missing.
    Measured on simplecss.org: `/assets/images/favicon.png` was referenced by
    all three mirrored pages and absent from the mirror.

    An extensionless or `.html` reference is a ROUTE, not an asset; those
    belong to `unlistedRoutes`, which reports them rather than silently
    widening a paid page count."""
    api = pw.request.new_context(ignore_https_errors=IGNORE_TLS)
    todo, seen = [], set()
    for f in sorted(OUT.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in (".html", ".htm", ".css"):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        own = SITE + "/" + str(f.relative_to(OUT)).replace(os.sep, "/")
        for m in REF_RE.finditer(text):
            ref = (m.group(1) or m.group(2) or m.group(3) or "").strip()
            if not ref or " " in ref or ref.startswith(("#", "data:", "mailto:", "tel:", "javascript:")):
                continue
            # Inline scripts contain `src=` inside string literals, and the
            # scanner cannot tell those from real attributes. A fragment of
            # JS resolved as a path and fetched produces a 404 that gets
            # reported against the CLIENT's site — a finding invented here,
            # blamed on them. These characters never occur in an authored
            # asset path but are everywhere in expressions.
            if re.search(r"[,(){}$+]", ref):
                continue
            try:
                u = urljoin(own, ref)
            except ValueError:
                continue
            if not u.startswith(("http://", "https://")):
                continue
            p = safe_urlparse(u)
            if p is None or p.netloc != SITE_HOST or Path(p.path).suffix.lower() in ("", ".html", ".htm"):
                continue
            key = canon(u)
            if key in captured or key in seen:
                continue
            seen.add(key)
            todo.append(u)

    got = 0
    for u in todo[:SWEEP_CEILING]:
        try:
            r = guarded_api_get(
                api, u, checker=request_is_private, timeout=20000,
                on_block=lambda blocked, reason: private_subrequests.append(
                    f"{blocked} ({reason})"
                ),
            )
        except Exception as e:
            warn(f"referenced asset {u} could not be fetched: {str(e).splitlines()[0]}")
            continue
        if r.status >= 400:
            report["unreachable"].append({"url": u, "status": r.status})
            continue
        ctype = r.headers.get("content-type")
        path = url_to_mirror_path(u, is_html(ctype), ctype)
        write_asset(path, r.body())
        captured[canon(u)] = link_target(ref_path(path))
        got += 1
    if len(todo) > SWEEP_CEILING:
        warn(f"{len(todo) - SWEEP_CEILING} referenced asset(s) past the sweep ceiling were not fetched")
    api.dispose()
    return got


# ---------------------------------------------------------------- serving

HEADER_SIDECAR = ".__headers.json"


def serve(directory, port=0):
    """No SPA fallback, deliberately — see url_to_mirror_path. A missing file
    must 404 here, because a 404 during gate -2 is the finding.

    Runtime endpoints are replayed with their recorded headers: the sidecar
    written at capture time carries content-type and the x-* headers the
    client code actually inspects. Extensionless files without a sidecar
    fall back to sniffing, because octet-stream turns a JSON reply into
    something no fetch().json() caller trusts."""
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def translate_path(self, path):
            """The stock lookup strips the query and answers 404 for every
            parameterised endpoint. The variants exist — captured under the
            same query hash the writer used — so a miss WITH a query gets one
            more chance: `<path>.<sha1(query)[:8]>.<ext>`."""
            p = super().translate_path(path)
            if not os.path.exists(p) and "?" in self.path:
                h = hashlib.sha1(self.path.split("?", 1)[1].encode()).hexdigest()[:8]
                hits = [c for c in glob.glob(f"{p}.{h}.*") if not c.endswith(HEADER_SIDECAR)]
                if hits:
                    return hits[0]
            return p

        def _sidecar(self):
            f = Path(self.translate_path(self.path) + HEADER_SIDECAR)
            try:
                return json.loads(f.read_text()) if f.is_file() else {}
            except (OSError, ValueError):
                return {}

        def guess_type(self, path):
            side = self._sidecar()
            if side.get("content-type"):
                return side["content-type"]
            base = super().guess_type(path)
            if base == "application/octet-stream" and os.path.isfile(path):
                try:
                    with open(path, "rb") as f:
                        head = f.read(64).lstrip()
                    if head[:1] in (b"{", b"["):
                        return "application/json"
                    if head[:1] == b"<":
                        return "text/html"
                except OSError:
                    pass
            return base

        def end_headers(self):
            for k, v in self._sidecar().items():
                if k != "content-type":
                    self.send_header(k, v)
            # A mirror under active iteration must never be served from the
            # browser's cache: SimpleHTTPRequestHandler sends Last-Modified,
            # the browser caches heuristically off it, and a page rebuilt on
            # disk keeps rendering as its WEEKS-OLD self with no request ever
            # reaching the server — seen live as "the old blog keeps coming
            # back" after the /blog page was replaced.
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

    handler = functools.partial(Handler, directory=str(directory))
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        # Taken by something else. A random free port still shows the mirror,
        # which beats exiting over a preference.
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        if port:
            print(f"- port {port} is in use, serving on {httpd.server_port} instead")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_port}"


def serve_until_interrupted(code):
    """Hand the finished mirror over as a URL, not as a directory nobody can
    open. Blocks until Ctrl-C, then exits with the gate's verdict so the exit
    code still means what it meant."""
    srv, url = serve(OUT, args.port)
    print(f"\n  mirror beží na  {url}/")
    print("  (Ctrl-C ukončí)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print()
    finally:
        srv.shutdown()
    sys.exit(code)


# ---------------------------------------------------------------- gate -2

def gate(routes, mirror_url):
    from PIL import Image, ImageChops
    shots = REPORT.parent / "mirror-parity"
    shots.mkdir(parents=True, exist_ok=True)
    ok = True
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for route in routes:
            page_report = report["pages"].setdefault(route, {})
            page_report["parity"] = {}
            key = route.strip("/").replace("/", "_") or "index"
            for label, w in WIDTHS:
                imgs, blocked = [], []
                for side, url in (("live", SITE + route), ("mirror", mirror_url + route)):
                    ctx = browser.new_context(viewport={"width": w, "height": 900},
                                              device_scale_factor=1, service_workers="block",
                                              ignore_https_errors=IGNORE_TLS)
                    if side == "live":
                        attach_network_guard(
                            ctx, checker=request_is_private,
                            on_block=lambda request, reason: private_subrequests.append(
                                f"{request.url} ({reason})"
                            ),
                        )
                    else:
                        # The mirror is judged OFFLINE. Anything the page
                        # itself still reaches for is a hole, and a hole
                        # that loads from the client's server looks like a
                        # pass. Third-party EMBEDS are the exception: a
                        # YouTube frame is meant to stay remote, so blocking
                        # it would fail a mirror that is in fact correct.
                        def offline(route):
                            req = route.request
                            req_url = urlparse(req.url)
                            mirror_origin = urlparse(mirror_url)
                            if (req_url.scheme, req_url.netloc) == (mirror_origin.scheme, mirror_origin.netloc):
                                return route.continue_()
                            private = request_is_private(req.url)
                            if private:
                                private_subrequests.append(f"{req.url} ({private})")
                                blocked.append(req.url)
                                return route.abort()
                            try:
                                if req.frame.parent_frame is not None:
                                    return route.continue_()
                            except Exception:
                                pass
                            blocked.append(req.url)
                            return route.abort()

                        ctx.route("**", offline)
                    p = ctx.new_page()
                    try:
                        p.goto(url, wait_until="commit", timeout=args.timeout)
                        settle(p)
                        path = shots / f"{key}.{label}.{side}.png"
                        p.screenshot(path=str(path), full_page=True)
                        imgs.append(path)
                    except Exception as e:
                        warn(f"gate -2 could not capture {side} {url}: {str(e).splitlines()[0]}")
                    ctx.close()
                for u in dict.fromkeys(blocked):
                    if urlparse(u).hostname not in ("127.0.0.1", "localhost"):
                        warn(f"{route} @{label} still reaches the network for {u}")
                if len(imgs) != 2:
                    ok = False
                    page_report["parity"][label] = None
                    continue
                a, b = Image.open(imgs[0]).convert("RGB"), Image.open(imgs[1]).convert("RGB")
                if a.size != b.size:
                    h = max(a.size[1], b.size[1])
                    pad = lambda im: (lambda c: (c.paste(im, (0, 0)), c)[1])(
                        Image.new("RGB", (max(a.size[0], b.size[0]), h), (255, 255, 255)))
                    a, b = pad(a), pad(b)
                diff = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 24 else 0)
                ratio = sum(diff.histogram()[1:]) / float(a.size[0] * a.size[1])
                page_report["parity"][label] = round(ratio, 5)
                if ratio > args.threshold:
                    ok = False
                    diff.save(str(shots / f"{key}.{label}.diff.png"))
                    print(f"  FAIL {route} @{label}: {ratio:.2%} differs from the live site")
                else:
                    print(f"  ok   {route} @{label}: {ratio:.2%}")
        browser.close()
    return ok


# ---------------------------------------------------------------- main

def bare_host(host):
    return host[4:] if host.startswith("www.") else host


def adopt_landing_origin(browser, first_route):
    """Mirror the origin the site ACTUALLY serves from, not the one typed.

    Half of real sites redirect apex to www or the other way, and an order
    arrives as whatever address the client pasted. Every consequence of
    getting this wrong is silent: the homepage lands under `_ext/www.host/`
    instead of the mirror root, every internal link counts as cross-origin
    and is left pointing at the client's live domain, and an `http://` arg
    that lands on https makes every capture key mismatch the rewriter's
    lookups. A scheme or www variant is adopted; a redirect to a genuinely
    different domain is refused out loud, because that is a different site."""
    global SITE, SITE_HOST
    ctx = browser.new_context(ignore_https_errors=IGNORE_TLS)
    attach_network_guard(
        ctx, checker=request_is_private,
        on_block=lambda request, reason: private_subrequests.append(
            f"{request.url} ({reason})"
        ),
    )
    page = ctx.new_page()
    try:
        page.goto(SITE + first_route, wait_until="commit", timeout=args.timeout)
        landed = urlparse(page.url)
    except Exception as e:
        print(f"{SITE} did not respond: {str(e).splitlines()[0]}", file=sys.stderr)
        sys.exit(2)
    finally:
        ctx.close()
    origin = f"{landed.scheme}://{landed.netloc}"
    if origin == SITE:
        return
    if bare_host(landed.netloc) != bare_host(SITE_HOST):
        print(f"{SITE} redirects to {origin} — a different domain, not a www or https "
              f"variant of it. Mirroring that is a decision, not a detail; re-run with "
              f"--site {origin} if it is the site you were given.", file=sys.stderr)
        sys.exit(2)
    # Adopting an origin means fetching everything from it, so it goes through
    # the same address check --site did. The domain test above is about
    # INTENT ("is this still the site you were given"); it says nothing about
    # where the new name resolves, and a public host redirecting to a www
    # variant that points inside the network would otherwise be adopted here
    # without a second look.
    refuse_private(origin, what="the redirect target")
    print(f"- {SITE} redirects to {origin} — adopting it as the origin")
    SITE, SITE_HOST = origin, landed.netloc
    report["site"] = SITE
    report["canonicalOrigin"] = SITE


def prepare_out():
    if OUT.exists() and any(OUT.iterdir()):
        if not (OUT / MARKER).exists() and not args.force:
            print(f"{OUT} is not empty and was not written by this script — pass --force",
                  file=sys.stderr)
            sys.exit(2)
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / MARKER).write_text(SITE + "\n")


def main():
    if args.serve_only:
        if not (OUT / "index.html").exists():
            print(f"--serve-only needs an existing mirror in {OUT}", file=sys.stderr)
            sys.exit(2)
        serve_until_interrupted(0)
    routes = [r.strip() for r in args.routes.split(",") if r.strip()] or ["/"]
    routes = [r if r.startswith("/") else "/" + r for r in routes]
    prepare_out()

    discovered = set()
    done = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        # Before anything is captured — every path and every rewrite key is
        # derived from the origin, so adopting it later would invalidate both.
        adopt_landing_origin(browser, routes[0])
        print(f"- mirroring {SITE} into {OUT}")
        queue = list(routes)
        while queue:
            route = queue.pop(0)
            if route in done:
                continue
            if args.crawl and len(done) >= args.max_pages:
                warn(f"crawl stopped at --max-pages={args.max_pages}; "
                     f"{len(queue) + 1} known route(s) not mirrored")
                break
            print(f"- {route}")
            done.append(route)
            mirror_route(browser, route, discovered)
            if args.crawl:
                for r in sorted(discovered):
                    if r not in done and r not in queue:
                        queue.append(r)
        swept = sweep_referenced(pw)
        if swept:
            print(f"- {swept} asset(s) referenced but never fetched by the browser, swept in")
        browser.close()

    report["routes"] = done
    report["assets"] = len(written)
    # Pages the site links to that were never part of the job. This is the
    # artifact that settles "you said ten pages" before delivery, not after.
    report["unlistedRoutes"] = sorted(r for r in discovered if r not in done)
    if report["unlistedRoutes"]:
        print(f"- {len(report['unlistedRoutes'])} linked route(s) not mirrored: "
              f"{', '.join(report['unlistedRoutes'][:8])}")

    changed = rewrite_pass()
    print(f"- {len(written)} file(s) written, {changed} rewritten to local paths")
    if not (OUT / "index.html").exists():
        warn("no index.html at the mirror root — stage -1 will refuse this directory")

    if args.no_verify:
        warn("gate -2 skipped (--no-verify) — mirror completeness is unmeasured")
    else:
        srv, mirror_url = serve(OUT)
        try:
            print("- gate -2: live site vs offline mirror")
            report["passed"] = gate(done, mirror_url)
        finally:
            srv.shutdown()
    if report["unreachable"]:
        # Reported loudly, but NOT a gate failure. An address the client's own
        # server answers with 404 is a broken link on their site, and a mirror
        # that reproduces it faithfully is doing its job. Failing the
        # conversion over it would make the client's bug look like ours —
        # this belongs in the handover conversation instead.
        print(f"- {len(report['unreachable'])} address(es) the LIVE site itself refuses "
              f"(broken there, not here — raise at handover):")
        for e in report["unreachable"][:8]:
            print(f"    {e['status']}  {e['url']}")

    if report["dynamicEndpoints"]:
        # One line per endpoint, not per variant: this is the site asking its
        # backend a parameterised question, which no static mirror can answer.
        print(f"- {len(report['dynamicEndpoints'])} parameterised endpoint(s) the site queries "
              f"at runtime — static hosting cannot answer these:")
        for e in report["dynamicEndpoints"][:5]:
            print(f"    {e}?<payload>")

    if private_subrequests:
        # Louder than the writes, because this one says something about the
        # PAGE rather than about this script: a site whose scripts reach for
        # 169.254.169.254 or a LAN address is a site whose owner wants to know.
        # Refused, so none of it reached the mirror or the conversion.
        report["blockedPrivateRequests"] = sorted(set(private_subrequests))
        print(f"- {len(report['blockedPrivateRequests'])} request(s) to private or "
              f"link-local addresses were refused:")
        for w in report["blockedPrivateRequests"][:5]:
            print(f"    {w}")
        if len(report["blockedPrivateRequests"]) > 5:
            print(f"    … and {len(report['blockedPrivateRequests']) - 5} more")
        print("  Nothing from those addresses is in the mirror. If this site is "
              "genuinely on your own network, re-run with H2WP_ALLOW_PRIVATE_MIRROR=1.")

    if blocked_writes:
        # Named, not buried: this is the script declining to act on the
        # client's live server, and the user is entitled to know what it was.
        report["blockedWrites"] = sorted(set(blocked_writes))
        print(f"- {len(report['blockedWrites'])} non-GET request(s) refused, so nothing was "
              f"written to the live site:")
        for w in report["blockedWrites"][:5]:
            print(f"    {w}")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2))
    print(f"\nreport: {REPORT}")
    if report["passed"]:
        print(f"mirror ready — next: python3 prerender-spa.py --skip-build --dist {OUT} "
              f"--project . --out static-src --routes={','.join(done)}")
    code = 0 if report["passed"] else 1
    if args.no_serve:
        sys.exit(code)
    serve_until_interrupted(code)


if __name__ == "__main__":
    main()
