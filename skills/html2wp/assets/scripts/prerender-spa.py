#!/usr/bin/env python3
"""Stage -1 — an SPA becomes the flat HTML this pipeline converts.

  python3 prerender-spa.py --project <dir> --out <static-dir>
      [--routes=/,/story,...] [--dist <dir>] [--skip-build]
      [--build-cmd "npm run build"] [--threshold 0.006] [--no-verify]
      [--report prerender-report.json] [--force]

`analyze-input.mjs` REFUSES a React/Vue SPA shell, and it is right to: one
HTML file with an empty `<div id="root">` has nothing 1:1 to convert. But
the refusal names a route out — "offer the prerender route if their
framework supports it" — and Lovable / Bolt / v0 / shadcn projects, the
input class this converter advertises, arrive in exactly that shape. This
is that route, made deterministic.

WHAT MAKES THIS DIFFERENT FROM `curl`-ING A RENDERED PAGE
---------------------------------------------------------
A naive prerender captures `document.documentElement.outerHTML` and ships
it. That loses, silently, everything the framework had not mounted at the
instant of capture — and every gate downstream then agrees with the loss,
because the loss is in the INPUT. Measured on the reference conversion (a
React wedding site): a bare capture dropped the entire mobile drawer
(`{open && <motion.div>}` is a conditional render — absent, not hidden) and
all eight FAQ answers (Radix `AccordionContent` unmounts closed panels).
Gate A compared prerender-with-no-answers against build-with-no-answers,
scored 0.0%, and went green. The owner would have discovered it by clicking.

So this script does not photograph the DOM. It DRIVES it, records the state
transitions the framework actually performs, and replays them from markup:

  1. every conditional subtree is opened once, captured, and re-inserted
     into the at-rest document `display:none` — so the words exist in the
     markup, which is what makes them editable in WordPress;
  2. the attribute deltas that accompany each transition (`aria-expanded`,
     `data-state`, class swaps) are recorded as DATA on the elements;
  3. a small generic runtime (`assets/spa-runtime.js`, emitted here) replays
     exactly those recorded deltas.

Nothing about the behaviour is authored. It is measured from the running
application and replayed verbatim, which is the only form of "keep the
interactivity" that does not amount to rewriting the client's site from
memory. Behaviour the script could not record is REPORTED, never faked.

WHY THE BUNDLE IS STRIPPED
--------------------------
The framework's own `<script type="module">` is removed, along with its
modulepreload hints. This is not an optimisation. If React re-mounts on the
converted page it re-renders `#root` from its own component tree and throws
away whatever the owner just edited in WordPress — the stored source becomes
decoration. A prerendered page is a page whose markup IS the site, which is
the same contract every other input to this pipeline satisfies.

THE GATE
--------
Every stage here ends in a gate, and this one owns the seam no later gate
can see: stages 2 through 5 all measure prerender→WordPress, so a defect
introduced BETWEEN the running app and the static capture is invisible to
all of them. This gate is therefore React→prerender: full-page screenshots
of the live application against the static capture, at 1440/820/390, per
route. Exit 0 = every route within threshold; 1 = a route drifted, named in
the report; 2 = refused (no routes found, build failed).

Written for React Router (`<Route path=…>` and the `createBrowserRouter`
object form). Any SPA whose routes can be listed with `--routes` works —
the recording and capture phases are framework-agnostic.
"""

import argparse, functools, json, os, re, shutil, subprocess, sys, threading, time
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from range_files import RangeFilesMixin  # noqa: E402  (HTTP Range: a page script can seek a video)
import sandbox  # noqa: E402
import source_assets  # noqa: E402
from net_guard import attach_network_guard  # noqa: E402

from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--project", required=True, help="the SPA project root (has package.json)")
ap.add_argument("--out", required=True, help="directory to write flat HTML into — the input to stage 0")
ap.add_argument("--asset-origin", default="", help="known original HTTPS origin for missing source images")
ap.add_argument("--routes", default="", help="comma-separated route paths; omit to discover from the source")
ap.add_argument("--dist", default="", help="build output dir (default <project>/dist)")
ap.add_argument("--build-cmd", default="npm run build")
ap.add_argument("--skip-build", action="store_true")
ap.add_argument("--no-verify", action="store_true", help="skip the React->prerender parity gate (never on a real conversion)")
ap.add_argument("--threshold", type=float, default=0.006, help="same 0.6%% as gate A")
ap.add_argument("--jobs", type=int, default=3, help="gate -1: widths measured at once (one browser each); every red pair is measured again alone; "
                "the capture: routes captured at once (one browser each)")
ap.add_argument("--flash", action="store_true",
                help="Flash: entrance motion is waited for at most --settle-cap-ms (6 s) per capture instead of 26 s")
ap.add_argument("--settle-cap-ms", type=int, default=0,
                help="longest wait for entrance motion to settle per capture (default 26000; 6000 with --flash)")
ap.add_argument("--report", default="", help="default: prerender-report.json beside --out")
ap.add_argument("--force", action="store_true", help="clear --out even without this script's marker")
# Re-verifying an existing capture is a first-class need, not a shortcut:
# recording + capture is a ~30 minute pass on a ten-route site, and a gate
# whose cost is a lost afternoon is a gate people learn to skip.
ap.add_argument("--gates-only", action="store_true",
                help="skip build/record/capture; run gate -1b and gate -1 against an existing --out")
args = ap.parse_args()

PROJECT = Path(args.project).resolve()
OUT = Path(args.out).resolve()
# How long a capture waits for entrance motion to settle. Full keeps the
# 26 s a 16-second hero zoom needed; Flash takes the page as it is after 6 s
# (a still-moving transform is warned about, never silent).
SETTLE_CAP_MS = args.settle_cap_ms or (6000 if args.flash else 26000)
DIST = Path(args.dist).resolve() if args.dist else PROJECT / "dist"
REPORT = Path(args.report).resolve() if args.report else OUT.parent / "prerender-report.json"
MARKER = ".prerender-spa"

report = {
    "project": str(PROJECT), "dist": str(DIST), "out": str(OUT), "routes": [], "pages": {},
    "warnings": [], "skippedRoutes": [], "passed": True,
}


def warn(msg):
    report["warnings"].append(msg)
    print(f"  warn: {msg}")


def guard_context(ctx, *owned_origins):
    """Allow our exact local server(s), but not other private destinations."""
    attach_network_guard(
        ctx,
        allowed_origins=owned_origins,
        on_block=lambda request, reason: warn(
            f"blocked private browser request {request.url} ({reason})"
        ),
    )


# ---------------------------------------------------------------- routes

def discover_routes():
    """Read the route table out of the source rather than guessing from the
    filesystem: an SPA's URLs are declared in one place and are frequently
    NOT its component filenames (`/story` is served by `OurStory.tsx`)."""
    found, dynamic = [], []
    src = PROJECT / "src"
    if not src.exists():
        return found, dynamic
    pat_jsx = re.compile(r"""<Route\s[^>]*\bpath\s*=\s*["']([^"']+)["']""")
    pat_obj = re.compile(r"""\bpath\s*:\s*["']([^"']+)["']""")
    for f in sorted(src.rglob("*")):
        if f.suffix not in (".tsx", ".jsx", ".ts", ".js"):
            continue
        text = f.read_text(errors="ignore")
        if "<Route" not in text and "createBrowserRouter" not in text:
            continue
        for m in list(pat_jsx.finditer(text)) + (
            list(pat_obj.finditer(text)) if "createBrowserRouter" in text else []
        ):
            p = m.group(1)
            if not p.startswith("/"):
                p = "/" + p
            # A parameterised route needs DATA to prerender (which id? which
            # slug?) and the route table has none. Its instances are the pages
            # the app links to (main() follows them); none is invented.
            if ":" in p or "*" in p:
                dynamic.append(p)
                continue
            if p not in found:
                found.append(p)
    return found, dynamic


def tanstack_param_routes(project):
    """The parameterised routes a TanStack Router / Start app declares by
    file name — src/routes/blog/$slug.tsx or the flat blog.$slug.tsx — as
    /blog/:slug. Its pages come from the build output with no pattern
    attached; these name the template they share."""
    base = Path(project) / "src" / "routes"
    out = []
    if not base.is_dir():
        return out
    for f in sorted(base.rglob("*")):
        if f.suffix not in (".tsx", ".jsx", ".ts", ".js") or f.is_symlink():
            continue
        rel = f.relative_to(base).with_suffix("").as_posix()
        segs = [seg for part in rel.split("/") for seg in part.split(".")]
        segs = [seg for seg in segs if seg and not seg.startswith("_") and not (seg.startswith("(") and seg.endswith(")"))
                and seg not in ("index", "route")]
        if not any(seg.startswith("$") and len(seg) > 1 for seg in segs) or "$" in segs:
            continue
        route = "/" + "/".join(":" + seg[1:] if seg.startswith("$") else seg for seg in segs)
        if route not in out:
            out.append(route)
    return out


def param_route_patterns(dynamic):
    """Regexes for the parameterised routes of the route table (/blog/:slug).
    A catch-all is not a page family and gets none."""
    out = []
    for route in dynamic:
        if "*" in route:
            continue
        parts = [("[^/]+" if p.startswith(":") else re.escape(p)) for p in route.strip("/").split("/")]
        out.append((route, re.compile("^/" + "/".join(parts) + "/?$")))
    return out


def linked_route_instances(hrefs, patterns, known):
    """The concrete pages behind parameterised routes: the paths the app's own
    links point at that match one. Nothing is invented — a slug no page links
    to is not captured, exactly as before. Returns (route, pattern) pairs."""
    found = []
    for href in hrefs:
        if not href or not href.startswith("/") or href.startswith("//"):
            continue
        path = href.split("#")[0].split("?")[0]
        path = path.rstrip("/") or "/"
        if path in known or "," in path or any(path == f for f, _ in found):
            continue
        for route, rx in patterns:
            if rx.match(path):
                found.append((path, route))
                break
    return found


# Pages of a parameterised route discovered through links, per run.
MAX_LINKED_ROUTES = 200


CATCHALL_PROBE = "/prerender-spa-404-probe"


def named_routes(text):
    """--routes, plus whether the app has a catch-all route.

    Naming the routes is the only way to prerender /product/:slug pages, and
    it used to cost the site its 404 page: the catch-all probe was added only
    when routes were discovered. Whether the app HAS a catch-all is still read
    from the source."""
    routes = [r.strip() for r in text.split(",") if r.strip()]
    has_catchall = CATCHALL_PROBE not in routes and any("*" in d for d in discover_routes()[1])
    return routes, has_catchall


def route_to_file(route):
    if route == CATCHALL_PROBE:
        return "404.html"
    r = route.strip("/")
    # A route may already BE a file. Not every client-rendered input is a
    # router-driven SPA: a multi-page site whose chrome is injected by a
    # plain script serves real `.html` addresses, and appending `.html` to
    # `/about.html` names a file that does not exist — while the SPA
    # fallback quietly answers `/about` with the FRONT page, so every route
    # would be captured as a copy of the home page.
    if r.endswith(".html"):
        return r
    return "index.html" if r == "" else f"{r}.html"


# ---------------------------------------------------------------- TanStack Start
#
# Lovable's generator moved from a Vite + React Router SPA to TanStack Start:
# file routes under src/routes, an SSR server built by Nitro, and — by default
# — NO static index.html at all. `npm run build` then leaves nothing this
# script can serve. The framework can render its own routes to static HTML,
# though (`tanstackStart.prerender`), with the same components the SSR server
# would run — so that is switched on in the ISOLATED BUILD COPY, never in the
# client's project, and the pages it writes are the routes.

def is_tanstack_start(project):
    try:
        pkg = json.loads((Path(project) / "package.json").read_text())
    except (OSError, ValueError):
        return False
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    return "@tanstack/react-start" in deps


TANSTACK_PRERENDER = "prerender: { enabled: true, crawlLinks: true }"


def enable_tanstack_prerender(work):
    """Turn on TanStack Start's static prerender in the build copy's Vite
    config. Returns what was done, or None when no config could be patched
    (the build then fails loudly on "no index.html", as before)."""
    for name in ("vite.config.ts", "vite.config.mts", "vite.config.js", "vite.config.mjs"):
        cfg = Path(work) / name
        if cfg.is_file() and not cfg.is_symlink():
            break
    else:
        return None
    text = cfg.read_text()
    if re.search(r"prerender\s*:\s*\{[^}]*enabled\s*:\s*false", text):
        new = re.sub(r"(prerender\s*:\s*\{[^}]*enabled\s*:\s*)false", r"\1true", text, count=1)
        how = "prerender.enabled flipped to true"
    elif re.search(r"\bprerender\s*:", text):
        return "prerender already configured — left as authored"
    elif re.search(r"tanstackStart\s*:\s*\{", text):
        new = re.sub(r"(tanstackStart\s*:\s*\{)", r"\1 " + TANSTACK_PRERENDER + ",", text, count=1)
        how = "prerender added to tanstackStart: {…}"
    elif re.search(r"tanstackStart\(\s*\{", text):
        new = re.sub(r"(tanstackStart\(\s*\{)", r"\1 " + TANSTACK_PRERENDER + ",", text, count=1)
        how = "prerender added to tanstackStart({…})"
    elif re.search(r"tanstackStart\(\s*\)", text):
        new = re.sub(r"tanstackStart\(\s*\)", "tanstackStart({ " + TANSTACK_PRERENDER + " })", text, count=1)
        how = "prerender added to tanstackStart()"
    elif "@lovable.dev/vite-tanstack-config" in text and re.search(r"defineConfig\(\s*\{", text):
        new = re.sub(r"(defineConfig\(\s*\{)", r"\1 tanstackStart: { " + TANSTACK_PRERENDER + " },", text, count=1)
        how = "tanstackStart.prerender added to the Lovable config"
    elif "@lovable.dev/vite-tanstack-config" in text and re.search(r"defineConfig\(\s*\)", text):
        new = re.sub(r"defineConfig\(\s*\)", "defineConfig({ tanstackStart: { " + TANSTACK_PRERENDER + " } })", text, count=1)
        how = "tanstackStart.prerender added to the Lovable config"
    else:
        return None
    cfg.write_text(new)
    return f"{cfg.name}: {how}"


def routes_from_output(dist):
    """The routes are the pages the framework wrote — including every
    `/blog/<slug>` its crawl reached, which no reading of src/routes can
    enumerate (the slugs live in data)."""
    found = []
    for page in sorted(Path(dist).rglob("*.html")):
        if page.is_symlink():
            continue
        rel = page.relative_to(dist).as_posix()
        if rel in ("404.html", "_shell.html") or rel.startswith(("_", "assets/")):
            continue
        if rel == "index.html":
            route = "/"
        elif rel.endswith("/index.html"):
            route = "/" + rel[: -len("/index.html")]
        else:
            route = "/" + rel[: -len(".html")]
        if "," not in route and route not in found:
            found.append(route)
    return found


TANSTACK = is_tanstack_start(PROJECT)


# ---------------------------------------------------------------- build

def build():
    global DIST

    def usable_prebuilt():
        """Return an existing regular output without following a symlink root."""
        for candidate in (DIST, PROJECT / "dist", PROJECT / "dist" / "client",
                          PROJECT / ".output" / "public", PROJECT / "build", PROJECT / "out"):
            if candidate.is_symlink():
                continue
            if candidate.is_dir() and (candidate / "index.html").is_file() \
                    and not (candidate / "index.html").is_symlink():
                return candidate
        return None

    def fall_back_or_stop(why, code):
        prebuilt = usable_prebuilt()
        if prebuilt is None:
            print(why, file=sys.stderr)
            print(f"  FAILED_WITH_ACTION ({code}): no safe prebuilt output exists. "
                  "Build the trusted project yourself and pass --dist, or install/start Docker.",
                  file=sys.stderr)
            sys.exit(2)
        print(f"  ! {why}", file=sys.stderr)
        print(f"  ! Using prebuilt output in {prebuilt.name}/. It is NOT a fresh build; "
              "check the pages before handover.", file=sys.stderr)
        return prebuilt

    def sandbox_output(work):
        candidates = []
        try:
            candidates.append(work / DIST.relative_to(PROJECT))
        except ValueError:
            pass
        # dist/client and .output/public: where TanStack Start / Nitro put
        # the prerendered pages (the plain dist/ holds only the server there).
        candidates.extend((work / "dist", work / "dist" / "client", work / ".output" / "public",
                           work / "build", work / "out"))
        for candidate in candidates:
            if candidate.is_symlink():
                continue
            if candidate.is_dir() and (candidate / "index.html").is_file() \
                    and not (candidate / "index.html").is_symlink():
                return candidate
        return None

    if args.skip_build:
        print("- build skipped (--skip-build)")
    else:
        install_timeout = int(os.environ.get("H2WP_NPM_TIMEOUT", "900"))
        build_timeout = int(os.environ.get("H2WP_BUILD_TIMEOUT", "1200"))
        no_sandbox = sandbox.reason_unavailable()
        if no_sandbox and not sandbox.unsafe_override():
            DIST = fall_back_or_stop(
                f"sandbox unavailable ({no_sandbox}); refusing to execute project code on the host",
                "SANDBOX_UNAVAILABLE",
            )
        elif sandbox.unsafe_override():
            sandbox.warn_unsandboxed("H2WP_NO_SANDBOX=1")
            if TANSTACK:
                # Never edited in place: the host build runs in the client's
                # own project directory.
                warn("TanStack Start without the sandbox: prerender is NOT enabled (it would mean "
                     "editing the client's vite config) — enable tanstackStart.prerender yourself "
                     "or build with Docker")
            host_can_build = True

            def host_step(command, timeout):
                return subprocess.run(command, shell=True, cwd=PROJECT, timeout=timeout)

            if not (PROJECT / "node_modules").exists():
                print("- installing dependencies")
                try:
                    result = host_step("npm install", install_timeout)
                except subprocess.TimeoutExpired:
                    DIST = fall_back_or_stop("npm install timed out", "INSTALL_TIMEOUT")
                    host_can_build = False
                else:
                    if result.returncode != 0:
                        DIST = fall_back_or_stop("npm install failed", "INSTALL_FAILED")
                        host_can_build = False
            if host_can_build:
                print(f"- {args.build_cmd}")
                try:
                    result = host_step(args.build_cmd, build_timeout)
                except subprocess.TimeoutExpired:
                    DIST = fall_back_or_stop("the host build timed out", "BUILD_TIMEOUT")
                else:
                    if result.returncode != 0:
                        DIST = fall_back_or_stop("the host build failed", "BUILD_FAILED")
        else:
            metadata_error = sandbox.validate_dependency_metadata(PROJECT)
            if metadata_error:
                DIST = fall_back_or_stop(
                    f"dependency acquisition refused: {metadata_error}",
                    "UNSAFE_DEPENDENCY_SOURCE",
                )
            else:
                try:
                    work, deps = sandbox.prepare_workspace(PROJECT)
                except (OSError, ValueError) as err:
                    DIST = fall_back_or_stop(f"could not create the isolated build copy: {err}",
                                             "SANDBOX_PREPARE_FAILED")
                else:
                    if TANSTACK:
                        how = enable_tanstack_prerender(work)
                        print(f"- TanStack Start: {how or 'no Vite config found to enable prerender in'}"
                              " (in the isolated build copy only)")
                        report["tanstackStart"] = how
                    print("- installing dependencies (scripts disabled)")
                    try:
                        result = sandbox.run_in_sandbox(
                            "npm install --ignore-scripts --no-audit --no-fund "
                            "--registry=https://registry.npmjs.org/",
                            deps, install_timeout, "npm install", network=True,
                        )
                    except subprocess.TimeoutExpired:
                        DIST = fall_back_or_stop("sandboxed npm install timed out", "INSTALL_TIMEOUT")
                    else:
                        if result.returncode != 0:
                            DIST = fall_back_or_stop("sandboxed npm install failed", "INSTALL_FAILED")
                        else:
                            try:
                                sandbox.promote_dependencies(deps, work)
                            except (OSError, ValueError) as err:
                                DIST = fall_back_or_stop(f"dependency staging failed: {err}",
                                                         "INSTALL_FAILED")
                            else:
                                print("- rebuilding dependency lifecycle scripts offline")
                                try:
                                    result = sandbox.run_in_sandbox(
                                        "npm rebuild --offline", work, install_timeout,
                                        "npm rebuild", network=False,
                                    )
                                except subprocess.TimeoutExpired:
                                    DIST = fall_back_or_stop("offline dependency rebuild timed out",
                                                             "INSTALL_TIMEOUT")
                                else:
                                    if result.returncode != 0:
                                        DIST = fall_back_or_stop("offline dependency rebuild failed",
                                                                 "INSTALL_FAILED")
                                    else:
                                        print(f"- {args.build_cmd} (offline)")
                                        try:
                                            result = sandbox.run_in_sandbox(
                                                args.build_cmd, work, build_timeout, "build", network=False,
                                            )
                                        except subprocess.TimeoutExpired:
                                            DIST = fall_back_or_stop("the sandboxed build timed out",
                                                                     "BUILD_TIMEOUT")
                                        else:
                                            if result.returncode != 0:
                                                DIST = fall_back_or_stop("the sandboxed build failed",
                                                                         "BUILD_FAILED")
                                            else:
                                                fresh = sandbox_output(work)
                                                if fresh is None:
                                                    DIST = fall_back_or_stop(
                                                        "the build produced no index.html",
                                                        "BUILD_OUTPUT_MISSING",
                                                    )
                                                else:
                                                    DIST = fresh
                                                    print(f"- using isolated build output {DIST}")
    if not (DIST / "index.html").exists():
        print(f"no index.html in {DIST} — pass --dist", file=sys.stderr)
        sys.exit(2)


# ---------------------------------------------------------------- serving

def serve(directory, spa_fallback):
    """A built SPA is only navigable when unknown paths fall back to
    index.html — that is what its dev/preview server does and what its
    router assumes. Serving it without the fallback 404s every route but
    `/`, which reads exactly like a broken build."""
    class Handler(RangeFilesMixin, SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send_head(self):
            if spa_fallback:
                path = self.translate_path(self.path)
                if not os.path.exists(path) and not Path(path).suffix:
                    self.path = "/index.html"
            return super().send_head()

    handler = functools.partial(Handler, directory=str(directory))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_port}"


# ---------------------------------------------------------------- page JS

# Element identity across phases is by nth-child PATH, never by a stamped
# attribute. The recording phase clicks things, and a framework re-render
# replaces DOM nodes — an expando or a data- attribute set before the click
# is simply gone afterwards. A path is computed fresh in whichever DOM is in
# front of us, so it survives re-renders; and because every record is applied
# to the AT-REST document at the end, paths are always read against the same
# baseline shape they were recorded against.
HELPERS = r"""
// A control that scrolls to a section is a link to that section. React's
// usual way of saying it is `document.getElementById(id).scrollIntoView()`,
// which moves the page without touching the URL — so the only witness to
// WHERE it went is this call. Kept by the recorder, read in
// record_interactions(); the page's own call still runs unchanged.
(() => {
  const own = Element.prototype.scrollIntoView;
  Element.prototype.scrollIntoView = function (...a) {
    if (this.id) window.__spaScrollTarget = this.id;
    return own.apply(this, a);
  };
})();
window.__spa = {
  // Resolves when the page has STOPPED changing: no DOM mutation for `quiet`
  // ms, every finite animation and transition finished, two frames painted —
  // or at `cap` ms, whichever comes first. The recorder used fixed sleeps
  // sized for the slowest case (700 ms after every click, 450 after every
  // scroll reset, 900 after every reload) and paid them on every control of
  // every page at two widths; most controls settle in a frame or two.
  quiet(cap, quiet) {
    return new Promise((resolve) => {
      const start = performance.now();
      let last = start;
      const obs = new MutationObserver(() => { last = performance.now(); });
      obs.observe(document.documentElement, { subtree: true, childList: true, attributes: true, characterData: true });
      const done = () => { obs.disconnect(); resolve(Math.round(performance.now() - start)); };
      const tick = () => {
        const now = performance.now();
        if (now - start >= cap) return done();
        const running = document.getAnimations().some((a) => {
          try {
            const t = a.effect && a.effect.getComputedTiming();
            return a.playState === 'running' && t && t.iterations !== Infinity;
          } catch (e) { return false; }
        });
        if (!running && now - last >= quiet) {
          requestAnimationFrame(() => requestAnimationFrame(done));
          return;
        }
        setTimeout(tick, 16);
      };
      requestAnimationFrame(() => requestAnimationFrame(tick));
    });
  },
  pathOf(el) {
    const parts = [];
    while (el && el.nodeType === 1 && el !== document.documentElement) {
      const p = el.parentElement;
      if (!p) break;
      parts.unshift(Array.prototype.indexOf.call(p.children, el));
      el = p;
    }
    return parts.join('.');
  },
  elAt(path) {
    let el = document.documentElement;
    if (path === '') return el;
    for (const i of path.split('.')) {
      el = el.children[+i];
      if (!el) return null;
    }
    return el;
  },
  attrs(el) {
    const o = {};
    for (const n of el.getAttributeNames()) o[n] = el.getAttribute(n);
    return o;
  },
  snapshot() {
    const rows = [];
    const walk = (el, path) => {
      rows.push([el, path, window.__spa.attrs(el)]);
      const kids = el.children;
      for (let i = 0; i < kids.length; i++) walk(kids[i], path === '' ? String(i) : path + '.' + i);
    };
    walk(document.documentElement, '');
    window.__spaBase = rows;
    window.__spaBaseSet = new Set(rows.map(r => r[0]));
    window.__spaInnerBefore = null;
    return rows.length;
  },
  // Inline styles a motion library leaves behind describe an animation's
  // final frame, not the design. Keeping `height: auto` / `opacity: 1` /
  // `transform: none` on a re-inserted panel is harmless; keeping a
  // mid-flight `transform: translateY(-12px)` bakes a frozen animation into
  // the markup. Strip the animated properties, keep everything else.
  cleanStyle(el) {
    const drop = ['transform', 'opacity', 'height', 'pointer-events', 'will-change', 'transform-origin'];
    for (const p of drop) el.style.removeProperty(p);
    return el.getAttribute('style') || '';
  },
  diff(triggerPath) {
    const trigger = window.__spa.elAt(triggerPath);
    const attrChanges = [];
    for (const [el, path, before] of window.__spaBase) {
      if (!el.isConnected) continue;
      const after = window.__spa.attrs(el);
      const names = new Set([...Object.keys(before), ...Object.keys(after)]);
      for (const n of names) {
        const b = before[n] === undefined ? null : before[n];
        const a = after[n] === undefined ? null : after[n];
        if (b !== a) attrChanges.push({ path, attr: n, off: b, on: a });
      }
    }
    // Added subtrees, top-level only: a node whose parent already existed.
    const panels = [];
    let triggerInner = null;
    const all = document.querySelectorAll('*');
    for (const el of all) {
      if (window.__spaBaseSet.has(el)) continue;
      const parent = el.parentElement;
      if (!parent || !window.__spaBaseSet.has(parent)) continue;
      if (trigger && trigger.contains(el)) { triggerInner = true; continue; }
      // where does it go, expressed against the baseline?
      let prev = el.previousElementSibling;
      while (prev && !window.__spaBaseSet.has(prev)) prev = prev.previousElementSibling;
      const clone = el.cloneNode(true);
      window.__spa.cleanStyle(clone);
      panels.push({
        parentPath: window.__spa.pathOf(parent),
        afterPath: prev ? window.__spa.pathOf(prev) : null,
        html: clone.outerHTML,
        style: clone.getAttribute('style') || '',
        text: (el.textContent || '').trim().slice(0, 120),
      });
    }
    // A label that only changes its TEXT ("Menu" -> "Close") adds no element,
    // so the loop above never sees it. The caller leaves the trigger's inner
    // as it was before the click. Only a pure text change counts: same tags,
    // attributes and nesting. Anything else (an inner icon's class) is
    // already an attribute change, and replaying the whole inner for it
    // would throw away the ids stamped inside the trigger.
    const before = window.__spaInnerBefore;
    if (!triggerInner && trigger && before !== null && before !== undefined
        && trigger.innerHTML !== before
        && window.__spa.skeleton(trigger.innerHTML) === window.__spa.skeleton(before)) {
      triggerInner = true;
    }
    // Removed subtrees, top-level only: a baseline node gone while its
    // baseline parent is still there. That is a disclosure that was OPEN at
    // rest (an accordion's first item, default-expanded) and the click
    // closed it — the mirror image of an added panel.
    const byPath = new Map(window.__spaBase.map(r => [r[1], r[0]]));
    const removed = [];
    for (const [el, path] of window.__spaBase) {
      if (el.isConnected || path === '') continue;
      const parentPath = path.includes('.') ? path.slice(0, path.lastIndexOf('.')) : '';
      const parent = byPath.get(parentPath);
      if (parent && parent.isConnected) {
        removed.push({ path, text: (el.textContent || '').trim().slice(0, 120) });
      }
    }
    return {
      attrChanges,
      panels,
      removed,
      triggerInnerOn: triggerInner && trigger ? trigger.innerHTML : null,
    };
  },
  /** Markup with its text masked out: tags, attributes and nesting only. */
  skeleton(html) {
    const t = document.createElement('template');
    t.innerHTML = html;
    const walk = (n) => [...n.children].map(e =>
      '<' + e.tagName + ' ' + [...e.attributes].map(a => a.name + '=' + JSON.stringify(a.value)).sort().join(' ')
      + '>' + walk(e) + '</>').join('');
    return walk(t.content);
  },
  classMap() {
    const m = {};
    const walk = (el, path) => {
      const c = el.getAttribute('class');
      if (c !== null) m[path] = c;
      const kids = el.children;
      for (let i = 0; i < kids.length; i++) walk(kids[i], path === '' ? String(i) : path + '.' + i);
    };
    walk(document.documentElement, '');
    return m;
  },
  candidates() {
    // Only real CONTROLS. An earlier version also took every `[data-state]`
    // element, which on a Radix accordion means the item wrapper AND the
    // heading AND the button — three nested candidates that all toggle the
    // same panel, recorded three times as three unrelated disclosures.
    const out = new Set();
    for (const el of document.querySelectorAll(
      'button,[role="button"],[aria-expanded],[aria-controls],summary'
    )) {
      if (el.tagName === 'A') continue;
      // `el.type` is NOT the test: a <button> with no type attribute reports
      // type "submit", and React writes exactly that for every
      // `<button onClick>`. Filtering on the property therefore excluded
      // every button in the application — verified live, the mobile drawer
      // trigger among them. Only an EXPLICIT type=submit, or membership in a
      // form, means "this posts rather than discloses".
      if (el.getAttribute('type') === 'submit') continue;
      if (el.closest('form')) continue;
      // A control that CHANGES THE APPLICATION is not a disclosure, and
      // clicking it to find out what it reveals is destructive.
      //
      // "Add to cart" is the case that taught this. The recorder clicked it on
      // every product page: the basket filled (twelve items, baked into every
      // captured page's header until capture started clearing storage), and
      // the toast that appeared was recorded as a disclosure with three
      // panels — so gate -1b then demanded that clicking Add to cart on the
      // STATIC page reveal a toast, which is neither possible nor desirable.
      // A toast is transient state, not page content; there is nothing here to
      // convert and nothing to replay.
      //
      // Matched on the control's own words, which is the only thing available
      // before clicking it.
      //
      // Only a control that SAYS it is one of those — its label starts with
      // the action and is short — and never one that declares itself a
      // disclosure. The unanchored words matched inside FAQ questions: "Can I
      // buy sessions as a gift?" was treated as a Buy button, never recorded,
      // and shipped as an accordion item that does not open (hit live on a
      // Lovable site). aria-expanded / aria-controls is the control announcing
      // that it shows and hides something; that is not a purchase.
      const says = (el.getAttribute('aria-label') || el.textContent || '').trim();
      const discloses = el.hasAttribute('aria-expanded') || el.hasAttribute('aria-controls');
      if (!discloses && says.length <= 40
          && /^(?:\W*)(?:add to (?:cart|bag|basket|tote)|buy(?: now| it)?|checkout|check out|place order|subscribe|remove|delete|clear)\b/i.test(says)) continue;
      out.add(el);
    }
    // Innermost wins: drop any candidate that contains another candidate, so
    // a wrapper that merely bubbles a click to the real control is not
    // recorded as a second disclosure of the same panel.
    const all = [...out];
    return all
      .filter(el => !all.some(other => other !== el && el.contains(other)))
      .map(el => ({
        path: window.__spa.pathOf(el),
        tag: el.tagName.toLowerCase(),
        label: (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 60),
        // The site's own chrome: the same control on every page.
        chrome: !!el.closest('header,footer,nav,[role="banner"],[role="contentinfo"],[role="navigation"]'),
      }));
  },
};
"""


def settle(page, motion_timeout=26000, quick=False):
    """A page has finished becoming itself when its fonts are ready, its
    lazy images are decoded, every scroll-triggered reveal has fired, and
    nothing is still animating.

    The last of those is what a naive prerender gets wrong. Entrance motion
    on a real design routinely runs for seconds — the reference site's hero
    ran a 16-SECOND zoom — and a capture taken before it lands writes the
    intermediate transform into the markup as though it were the design.
    Stability is therefore measured, not assumed: sample the inline styles
    plus the computed transform of everything animating, and wait until the
    sample stops changing."""
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts && document.fonts.ready")
    # `scroll-behavior: smooth` turns every scrollTo below into an animation
    # that outlives the step delay, so the scroll-through never reaches the
    # bottom and half the reveals never fire. Nothing renders differently
    # with it off; it only exists for human scrolling.
    page.evaluate("() => { document.documentElement.style.scrollBehavior = 'auto'; }")
    # An expando, not a data- attribute: an attribute would be serialised
    # into the delivered markup, and this is scaffolding, not content.
    page.evaluate("""() => {
      for (const i of document.querySelectorAll('img[loading=lazy]')) { i.__spaWasLazy = true; i.loading = 'eager'; }
    }""")

    sig_js = """() => {
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

    # Stable means BOTH: the sampled styles stopped changing, and no finite
    # animation is running or waiting out its delay. The second condition is
    # what lets the samples come faster (150 ms instead of 300) without
    # mistaking a delayed entrance for a finished one — a WAAPI or CSS
    # animation in its delay phase is `running` and reported here, where the
    # style sample alone would read it as still.
    busy_js = """() => document.getAnimations().some((a) => {
      try {
        const t = a.effect && a.effect.getComputedTiming();
        return (a.playState === 'running' || a.pending) && t && t.iterations !== Infinity;
      } catch (e) { return false; }
    })"""

    def wait_motion():
        last, stable, waited = None, 0, 0
        while waited < motion_timeout:
            sig = page.evaluate(sig_js)
            busy = page.evaluate(busy_js)
            stable = stable + 1 if (sig == last and not busy) else 0
            last = sig
            if stable >= 3:
                return True
            page.wait_for_timeout(150)
            waited += 150
        return False

    # Scroll-through FIRST, then wait once. whileInView reveals only fire
    # after the element has been on screen, and entrance motion runs
    # concurrently with them — so a single wait placed after the scroll
    # covers both. Waiting before the scroll as well doubles the cost of
    # every capture (the reference site's 16s hero made that ~40s a page,
    # ~40 minutes across the gate) and proves nothing the later wait does not.
    if quick:
        # Recording only needs a mounted, clickable DOM. Reveal wrappers
        # animate their children's opacity; they do not unmount them, so
        # nothing below the fold is missing from the tree at this point.
        quiesce(page, 900, 150)
        return

    page.evaluate("""async () => {
      const h = document.body.scrollHeight;
      for (let y = 0; y < h; y += 600) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 90)); }
      window.scrollTo(0, 0);
    }""")
    if not wait_motion():
        warn(f"entrance motion never settled within {motion_timeout // 1000}s — capture may hold a mid-animation transform")

    # Bytes-arrived is not raster-exists: full-page capture paints far
    # outside the viewport and Chromium decodes lazily. Same discipline as
    # gate A's settle().
    waited, pending = 0, []
    while waited < 15000:
        pending = page.evaluate(
            "() => [...document.querySelectorAll('img')].filter(i => !i.complete).map(i => i.currentSrc || i.src)"
        )
        if not pending:
            break
        page.wait_for_timeout(250)
        waited += 250
    if pending:
        warn(f"{len(pending)} image(s) never loaded: {pending[:3]}")
    page.evaluate("""async () => {
      await Promise.all([...document.querySelectorAll('img')].map(i => i.decode().catch(() => {})));
    }""")
    page.wait_for_timeout(200)


# ---------------------------------------------------------------- recording

def quiesce(page, cap_ms, quiet_ms=120):
    """Wait for the page to stop changing, never longer than the fixed sleep
    it replaces (see window.__spa.quiet). The cap IS the old sleep, so a page
    that never goes quiet costs exactly what it always did."""
    try:
        page.evaluate("([c, q]) => window.__spa.quiet(c, q)", [cap_ms, quiet_ms])
    except Exception:
        page.wait_for_timeout(cap_ms)


def settle_scroll(page):
    """Return the page to scroll 0 and let scroll-reactive state catch up.

    Anything a component does BECAUSE the page moved is not part of the
    transition being recorded, and a scroll listener is a state update like
    any other — it needs a frame or two after the scroll to land."""
    page.evaluate("() => { document.documentElement.style.scrollBehavior = 'auto'; window.scrollTo(0, 0); }")
    quiesce(page, 450, 100)


# Flash: what each control of the site's chrome (header, footer, nav) did
# the first time it was driven, per width — {(width, path, tag, label):
# ("record", rec) | ("link", link) | ("inert", None)}. The same header is
# the same header on every page: driving it again on each of forty pages
# cost most of a page's recording and told nothing new.
CHROME_SEEN = {}


def record_interactions(page, url, widths=(390, 1440)):
    """Drive every disclosure control the page has, at each width, and write
    down what it did. Both widths matter and neither is optional: a mobile
    drawer's trigger is `lg:hidden`, so at 1440 it cannot be clicked at all,
    and a desktop-only disclosure is equally invisible at 390. In Flash a
    chrome control already driven on an earlier page is taken from there."""
    records, links, seen = [], [], set()
    for w in widths:
        page.set_viewport_size({"width": w, "height": 900})
        page.goto(url, wait_until="networkidle")
        settle(page, quick=True)
        cands = page.evaluate("() => window.__spa.candidates()")
        for c in cands:
            key = c["path"]
            if key in seen:
                continue
            chrome_key = (w, c["path"], c["tag"], c["label"]) if args.flash and c.get("chrome") else None

            def verdict(kind, value=None):
                if chrome_key:
                    CHROME_SEEN[chrome_key] = (kind, value)

            known = CHROME_SEEN.get(chrome_key) if chrome_key else None
            if known:
                if known[0] == "record":
                    seen.add(key)
                    records.append({**known[1], "reused": True})
                elif known[0] == "link":
                    seen.add(key)
                    links.append(dict(known[1]))
                continue
            # Baseline and diff are both taken at scroll 0. A disclosure's
            # recorded delta must describe THE DISCLOSURE — but clicking a
            # control routinely scrolls the page (focus scroll, or the
            # component pulling itself into view), and a scroll-reactive
            # header then swaps its classes in the same tick. Verified live:
            # every FAQ accordion trigger recorded the HEADER's transparent→
            # opaque swap as part of "open this answer", which (a) made the
            # runtime swap the header whenever a visitor opened a question
            # and (b) stamped ids into shared chrome, splitting that one page
            # off into a third header design group and a template part of its
            # own. Normalising scroll on both sides removes the whole class
            # of contamination.
            settle_scroll(page)
            page.evaluate("() => window.__spa.snapshot()")
            handle = page.evaluate_handle("(p) => window.__spa.elAt(p)", c["path"])
            el = handle.as_element()
            if el is None:
                continue
            try:
                if not el.is_visible():
                    continue
                before_inner = el.evaluate("e => (window.__spaInnerBefore = e.innerHTML)")
                before_url = page.url
                page.evaluate("() => { window.__spaScrollTarget = null; }")
                el.click(timeout=2500)
            except Exception:
                continue
            quiesce(page, 700)
            if page.url != before_url:
                # A control that navigates is a link wearing a button's
                # clothes; it discloses nothing. Before this was written down
                # it was only skipped, and every such control shipped as a
                # <button> with nothing behind it — a section menu that did
                # nothing on any converted page. Off-site stays a button (no
                # href to give it), and the page is reloaded either way: the
                # recorder's helpers do not exist on the page it landed on.
                to = link_target(page, url, before_url)
                if to:
                    seen.add(key)
                    links.append({"trigger": c["path"], "label": c["label"], "to": to})
                    verdict("link", links[-1])
                else:
                    warn(f"{c['label'] or c['path']}: navigates off-site or to a route not in the route table ({page.url}) — left as a button")
                    verdict("inert")
                page.goto(url, wait_until="networkidle")
                settle(page, quick=True)
                continue
            target = link_target(page, url, before_url)
            if target:
                wait_scroll_rest(page)
            settle_scroll(page)
            d = page.evaluate("(p) => window.__spa.diff(p)", c["path"])
            if target and is_scroll_link(page, url, target, d):
                seen.add(key)
                links.append({"trigger": c["path"], "label": c["label"], "to": target})
                verdict("link", links[-1])
                page.goto(url, wait_until="networkidle")
                settle(page, quick=True)
                continue
            # `style` is dropped from what is stored (below), so it cannot
            # make a control count either: a button whose only change is a
            # press animation's inline transform does nothing in the
            # original and must not ship as a toggle that flips aria-expanded.
            if (not d["panels"] and not d["triggerInnerOn"] and not d["removed"]
                    and not any(a["attr"] != "style" for a in d["attrChanges"])):
                verdict("inert")
                continue  # inert candidate — the wide net doing its job
            seen.add(key)
            if d["removed"] and not d["panels"]:
                # OPEN at rest; the click closed it. Recorded the right way
                # round — "on" is the resting (open) state, the panel is the
                # element already in the markup — or the runtime replays it
                # backwards: an item that cannot be closed and a label that
                # says the opposite of what is shown.
                records.append({
                    "trigger": c["path"], "label": c["label"], "width": w, "chrome": bool(c.get("chrome")),
                    "panels": [], "startsOpen": True,
                    "openPanels": [r["path"] for r in d["removed"]],
                    "openText": d["removed"][0]["text"],
                    "attrChanges": [{"path": a["path"], "attr": a["attr"], "off": a["on"], "on": a["off"]}
                                    for a in d["attrChanges"] if a["attr"] != "style"],
                    "triggerInner": ({"off": d["triggerInnerOn"], "on": before_inner}
                                     if d["triggerInnerOn"] is not None else None),
                })
                verdict("record", records[-1])
                page.goto(url, wait_until="networkidle")
                settle(page, quick=True)
                continue
            records.append({
                "trigger": c["path"], "label": c["label"], "width": w, "chrome": bool(c.get("chrome")),
                "panels": d["panels"],
                "attrChanges": [a for a in d["attrChanges"] if a["attr"] != "style"],
                "triggerInner": ({"off": before_inner, "on": d["triggerInnerOn"]}
                                 if d["triggerInnerOn"] is not None else None),
            })
            verdict("record", records[-1])
            if target:
                # is_scroll_link() reloaded the page to measure the scroll on
                # its own; `el` belongs to the page that is gone.
                page.goto(url, wait_until="networkidle")
                settle(page, quick=True)
                continue
            # Restore. Radix and every hand-rolled toggle close on a second
            # click; anything that does not gets a reload, because recording
            # the NEXT control against a dirty baseline produces a diff that
            # describes two transitions at once.
            try:
                el.click(timeout=2500)
                quiesce(page, 500)
            except Exception:
                pass
            clean = page.evaluate("() => document.querySelectorAll('*').length === window.__spaBase.filter(r => r[0].isConnected).length")
            if not clean:
                page.goto(url, wait_until="networkidle")
                settle(page, quick=True)
    # The same links inside a closed drawer cannot be clicked at any width
    # (they are there, but invisible until the drawer opens), so they would
    # stay dead buttons while their visible twins became links. A script
    # click reaches the component's handler without needing a visible box.
    page.set_viewport_size({"width": widths[-1], "height": 900})
    page.goto(url, wait_until="networkidle")
    settle(page, quick=True)
    for c in page.evaluate("() => window.__spa.candidates()"):
        if c["tag"] != "button" or c["path"] in seen:
            continue
        hidden_key = ("hidden", c["path"], c["tag"], c["label"]) if args.flash and c.get("chrome") else None
        known = CHROME_SEEN.get(hidden_key) if hidden_key else None
        if known:
            if known[0] == "link":
                seen.add(c["path"])
                links.append(dict(known[1]))
            continue
        if hidden_key:
            CHROME_SEEN[hidden_key] = ("inert", None)
        settle_scroll(page)
        page.evaluate("() => window.__spa.snapshot()")
        before_url = page.url
        if not page.evaluate("(p) => { const e = window.__spa.elAt(p); if (!e) return false;"
                             " window.__spaScrollTarget = null; e.click(); return true; }", c["path"]):
            continue
        quiesce(page, 700)
        to = link_target(page, url, before_url)
        if page.url != before_url:
            if to:
                seen.add(c["path"])
                links.append({"trigger": c["path"], "label": c["label"], "to": to})
                if hidden_key:
                    CHROME_SEEN[hidden_key] = ("link", links[-1])
            page.goto(url, wait_until="networkidle")
            settle(page, quick=True)
            continue
        if to:
            wait_scroll_rest(page)
            settle_scroll(page)
            d = page.evaluate("(p) => window.__spa.diff(p)", c["path"])
            if is_scroll_link(page, url, to, d):
                seen.add(c["path"])
                links.append({"trigger": c["path"], "label": c["label"], "to": to})
                if hidden_key:
                    CHROME_SEEN[hidden_key] = ("link", links[-1])
            page.goto(url, wait_until="networkidle")
            settle(page, quick=True)
            continue
        clean = page.evaluate("() => document.querySelectorAll('*').length === window.__spaBase.filter(r => r[0].isConnected).length"
                              " && window.__spa.diff('').attrChanges.length === 0")
        if not clean:
            page.goto(url, wait_until="networkidle")
            settle(page, quick=True)
    return records, links


def is_scroll_link(page, url, target, d):
    """A control that scrolled to a section is a LINK only if scrolling is all
    it did. An accordion that opens its panel and then pulls it into view
    calls scrollIntoView too — turned into an <a>, it would open nothing.

    What the scroll ALONE changes (a scroll-spy moving the "active section"
    underline, a header going opaque) is measured by doing just that scroll
    on a fresh load; the click is a link when its changes are all of that
    kind. Reloads the page — the caller must not reuse element handles."""
    if d["panels"] or d["triggerInnerOn"] is not None:
        return False
    # `style` is left out on both sides, as a recorded disclosure leaves it
    # out: scrolling runs reveal-on-scroll animations, and where each one is
    # stopped mid-frame differs from one measurement to the next.
    changes = [a for a in d["attrChanges"] if a["attr"] != "style"]
    if not changes:
        return True
    page.goto(url, wait_until="networkidle")
    settle(page, quick=True)
    settle_scroll(page)
    page.evaluate("() => window.__spa.snapshot()")
    section = target.split("#", 1)[1]
    if not page.evaluate("(id) => { const e = document.getElementById(id); if (!e) return false;"
                         " e.scrollIntoView(); return true; }", section):
        return False
    wait_scroll_rest(page)
    # At the section AND back at the top: a scroll-spy's "active" mark is
    # whatever the last section it saw was, so either state can be what the
    # click left behind once the recorder returned to scroll 0.
    key = lambda a: (a["path"], a["attr"], a["on"])
    by_scroll = {key(a) for a in page.evaluate("() => window.__spa.diff('')")["attrChanges"]}
    settle_scroll(page)
    by_scroll |= {key(a) for a in page.evaluate("() => window.__spa.diff('')")["attrChanges"]}
    return all(key(a) in by_scroll for a in changes)


def wait_scroll_rest(page, limit_ms=4000):
    """A smooth scroll to a section takes longer than any fixed pause; wait
    until the page stops moving, so what a scroll-spy shows is the state AT
    the section rather than one it passed on the way."""
    last, still, waited = None, 0, 0
    while waited < limit_ms and still < 3:
        y = page.evaluate("() => window.scrollY")
        still = still + 1 if y == last else 0
        last = y
        page.wait_for_timeout(100)
        waited += 100


def link_target(page, url, before_url):
    """Where a click just took the visitor, as a root-relative href — or None
    when it went nowhere. Two shapes: the router changed the URL (`/#about`
    from the blog), or the page scrolled itself to a section without
    touching the URL (the same control on the home page). The second is
    written as `<this page>#<id>`, which a browser replays natively."""
    if page.url != before_url:
        u = urlparse(page.url)
        if urlparse(url).netloc != u.netloc:
            return None
        # Only a page the conversion has. A route nobody discovered becomes
        # an <a> to a URL WordPress answers with 404 — and no gate follows
        # links, so a dead button is the honest (and reported) outcome.
        path = u.path or "/"
        if KNOWN_ROUTES and path not in KNOWN_ROUTES and path.rstrip("/") not in KNOWN_ROUTES:
            return None
        return path + (("#" + u.fragment) if u.fragment else "")
    target = page.evaluate("() => window.__spaScrollTarget")
    if target and re.fullmatch(r"[A-Za-z][\w-]*", target):
        # A carousel "next" calls scrollIntoView on its slides too. A link
        # would jump the whole page instead of sliding the strip, so a
        # target inside a horizontally scrolling box is not a section.
        in_strip = page.evaluate("""(id) => {
          let e = document.getElementById(id);
          for (e = e && e.parentElement; e && e !== document.body; e = e.parentElement) {
            const ox = getComputedStyle(e).overflowX;
            if ((ox === 'auto' || ox === 'scroll') && e.scrollWidth > e.clientWidth) return true;
          }
          return false; }""", target)
        if in_strip:
            return None
        return (urlparse(url).path or "/") + "#" + target
    return None


# Filled by main() from the route table; empty = unknown, no filtering.
KNOWN_ROUTES = set()


def scope_group_changes(records):
    """In a single-select group, each item was recorded against a baseline
    where ANOTHER item may have been open (an accordion whose first item is
    open at rest). The app closed that item as a side effect, and the change
    landed in this item's record — so closing this item later restored the
    other one's "open" attributes (aria-expanded back to true on an item whose
    panel the group had just hidden). The group logic opens and closes the
    siblings itself; an item's record keeps only what belongs to it."""
    by_group = {}
    for r in records:
        if r.get("group"):
            by_group.setdefault(r["group"], []).append(r)
    for members in by_group.values():
        for r in members:
            own = r["trigger"]
            others = [m["trigger"] for m in members if m is not r]

            def foreign(path):
                for o in others:
                    if path == o or path.startswith(o + "."):
                        return True  # the other item's trigger or inside it
                    if o.startswith(path + ".") and not own.startswith(path + "."):
                        return True  # an ancestor of the other item only
                return False
            r["attrChanges"] = [a for a in r["attrChanges"] if not foreign(a["path"])]


def detect_single_select(page, url, records):
    """A group of disclosures is single-select when opening one closes the
    last. It cannot be inferred from one control at a time — each was
    recorded against a clean baseline on purpose — so it needs its own
    probe: open A, then open B, and see whether A's panel survived.

    Skipping this is how a converted accordion ends up with every panel
    open at once the moment a visitor clicks twice."""
    groups = {}
    # A chrome group taken from an earlier page keeps its verdict and its
    # name; new groups are numbered after it, never onto it.
    for r in records:
        if r.get("reused") and r.get("group"):
            groups.setdefault(r["group"], []).append(r["trigger"])
    taken = [int(g[1:]) for g in groups if g[1:].isdigit()]
    # Structural siblings, not DOM siblings. A Radix accordion nests its
    # trigger two levels inside the item (item > h3 > button), so the
    # triggers are never each other's siblings and a parent-path key puts
    # each one in a group of its own — which reads as "no group" and ships
    # an accordion that opens every panel at once. Two triggers belong to one
    # group when their paths have the same LENGTH and differ in exactly one
    # segment: that is precisely "the same control, one repeat over".
    buckets = []
    for r in records:
        if not r["panels"] and not r.get("startsOpen"):
            continue
        segs = r["trigger"].split(".")
        placed = False
        for b in buckets:
            ref = b[0]["trigger"].split(".")
            if len(ref) != len(segs):
                continue
            if sum(1 for x, y in zip(ref, segs) if x != y) == 1:
                b.append(r)
                placed = True
                break
        if not placed:
            buckets.append([r])
    gid = max(taken, default=0)
    for rs in buckets:
        if len(rs) < 2 or all(r.get("reused") for r in rs):
            continue
        # Probe with two CLOSED-at-rest items: clicking an open-at-rest one
        # closes it, which would read as "A did not survive" for any
        # accordion, single-select or not.
        closed = [r for r in rs if not r.get("startsOpen")]
        if len(closed) < 2:
            continue
        a, b = closed[0], closed[1]
        page.set_viewport_size({"width": max(r["width"] for r in rs), "height": 900})
        page.goto(url, wait_until="networkidle")
        settle(page, quick=True)
        try:
            for r in (a, b):
                h = page.evaluate_handle("(p) => window.__spa.elAt(p)", r["trigger"])
                e = h.as_element()
                if e is None or not e.is_visible():
                    raise RuntimeError("not clickable")
                e.click(timeout=2500)
                quiesce(page, 600)
        except Exception:
            continue
        # is A's panel still there?
        still = page.evaluate(
            "(t) => { const el = window.__spa.elAt(t); return !!(el && (el.getAttribute('aria-expanded') === 'true' || el.getAttribute('data-state') === 'open')); }",
            a["trigger"],
        )
        if not still:
            gid += 1
            for r in rs:
                r["group"] = f"g{gid}"
            groups[f"g{gid}"] = [r["trigger"] for r in rs]
    return groups


def detect_close_on_link(page, url, records, links):
    """Does following a same-page link inside an open disclosure close it?

    A drawer's section items typically call `setOpen(false)` and scroll. On
    the converted page the scroll is the browser's own (a hash link), so
    nothing closes the drawer unless the runtime is told to — and on the home
    page it then stays open over the section the visitor asked for. Only
    what the application was SEEN to do is replayed: open the disclosure,
    script-click one same-page hash link inside it, and read whether the
    disclosure's own attributes went back to `off`.

    Sets `closeOnLink` on each probed record to True/False; leaves it unset
    (unknown) where the panel holds no same-page hash link to try — on every
    page but the one the sections live on, the same drawer items navigate."""
    here = urlparse(url).path or "/"
    same_page = [l for l in links if "#" in l["to"] and l["to"].split("#", 1)[0] == here]
    probed = False
    for r in records:
        if r.get("reused") and "closeOnLink" in r:
            continue
        # The scope a link must sit in. A changed element that CONTAINS the
        # trigger is the chrome around it (a header going solid), not a panel.
        scope = [a["path"] for a in r["attrChanges"]
                 if not r["trigger"].startswith(a["path"] + ".") and a["path"] != r["trigger"]]
        if r["panels"] or not scope:
            # Inserted panels have no baseline path to find a link by; the
            # recorded links were all taken from the baseline DOM.
            continue
        link = next((l for l in same_page
                     if any(l["trigger"].startswith(s + ".") for s in scope)), None)
        if not link:
            continue
        page.set_viewport_size({"width": r["width"], "height": 900})
        page.goto(url, wait_until="networkidle")
        probed = True
        settle(page, quick=True)
        settle_scroll(page)
        page.evaluate("() => window.__spa.snapshot()")
        try:
            e = page.evaluate_handle("(p) => window.__spa.elAt(p)", r["trigger"]).as_element()
            if e is None or not e.is_visible():
                continue
            e.click(timeout=2500)
        except Exception:
            continue
        quiesce(page, 700)
        is_state = """([changes, side]) => changes.every(c => {
          const el = window.__spa.elAt(c.path);
          return el && el.getAttribute(c.attr) === c[side];
        })"""
        if not page.evaluate(is_state, [r["attrChanges"], "on"]):
            continue  # did not reopen the way it was recorded — no evidence
        if not page.evaluate("(p) => { const e = window.__spa.elAt(p); if (!e) return false;"
                             " e.click(); return true; }", link["trigger"]):
            continue
        quiesce(page, 700)
        wait_scroll_rest(page)
        if (urlparse(page.url).path or "/") != here:
            continue
        settle_scroll(page)
        r["closeOnLink"] = page.evaluate(is_state, [r["attrChanges"], "off"])
    if probed:
        page.goto(url, wait_until="networkidle")
        settle(page, quick=True)


# What a VALID submit shows, when the app shows it by itself. A component form
# ("Subscribe", "Send") confirms in script — a toast, a "Thanks!" line, the
# form swapped for a message — and a static capture holds none of it. Each
# form is filled with plausible values and submitted ONCE with every request
# blocked; only when the app attempted no request at all (the success was
# decided in the browser) is what appeared recorded. A form that posts
# somewhere is left alone: what it would show depends on an answer this run
# must never ask for, and a blocked request's error is not the design's
# success. The record rides on the <form> as data-spa-success (see capture),
# for whichever target connects that form to a real endpoint.
FORM_FILL_JS = r"""(i) => {
  const f = document.forms[i];
  if (!f) return { ok: false, why: 'gone' };
  if (f.getAttribute('role') === 'search' || [...f.elements].some((e) => e.type === 'search' || /^(s|q|search)$/i.test(e.name || ''))) {
    return { ok: false, why: 'search' };
  }
  const setVal = (el, v) => {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : el.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    const d = Object.getOwnPropertyDescriptor(proto, 'value');
    if (d && d.set) d.set.call(el, v); else el.value = v;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  let filled = 0;
  for (const el of f.elements) {
    const t = (el.type || '').toLowerCase();
    if (el.disabled || ['hidden', 'submit', 'button', 'reset', 'image', 'file'].includes(t) || el.tagName === 'BUTTON' || el.tagName === 'FIELDSET') continue;
    // Words, so a hint below can be matched as one: phone_number, user-email
    // and phoneNumber split the way a person reads them.
    const hint = [el.name, el.id, el.getAttribute('aria-label'), el.placeholder].map((x) => String(x || '')
      .replace(/([a-z])([A-Z])/g, '$1 $2').replace(/[_\-\[\].]+/g, ' ')).join(' ').toLowerCase();
    if (t === 'checkbox') { if (el.required && !el.checked) el.click(); continue; }
    if (t === 'radio') { if (!f.querySelector(`input[type=radio][name="${CSS.escape(el.name)}"]:checked`)) el.click(); continue; }
    if (el.tagName === 'SELECT') { const o = [...el.options].find((o) => o.value && !o.disabled); if (o) setVal(el, o.value); filled++; continue; }
    // Long enough for a "at least N characters" rule, and never shorter
    // than the field's own minimum.
    //
    // The field's TYPE decides first, and a hint counts only as a whole word:
    // a placeholder is a sentence, and "Tell me about the project" contains
    // "tel" — as a substring it made a textarea a phone number, nine
    // characters long, which the app's "at least 10 characters" refused (and
    // "Hotel", "Mailing address" were a phone and an email the same way).
    let v = 'Alex Test Visitor';
    const word = (re) => re.test(hint);
    if (t === 'email') v = 'visitor@example.com';
    else if (t === 'tel') v = '+15550100';
    else if (t === 'url') v = 'https://example.com';
    else if (t === 'number' || t === 'range') v = String(el.min || 1);
    else if (t === 'date') v = '2030-01-15';
    else if (el.tagName === 'TEXTAREA') v = 'Hello, this is a test message about your work.';
    else if (word(/\be ?mail\b/)) v = 'visitor@example.com';
    else if (word(/\b(phone|tel|telephone|mobile|cell)\b/)) v = '+15550100';
    else if (word(/\b(message|comment|question|details?|notes?|enquiry|inquiry)\b/)) v = 'Hello, this is a test message about your work.';
    if (el.minLength > 0 && v.length < el.minLength) v = v.padEnd(el.minLength, '.');
    if (el.maxLength > 0 && v.length > el.maxLength) v = v.slice(0, el.maxLength);
    setVal(el, v); filled++;
  }
  return { ok: filled > 0 && f.checkValidity(), why: filled ? 'invalid' : 'empty' };
}"""

FORM_WATCH_JS = r"""(i) => {
  const f = document.forms[i];
  window.__spaAdded = [];
  window.__spaFormGone = false;
  const obs = new MutationObserver((muts) => {
    for (const m of muts) for (const n of m.addedNodes) if (n.nodeType === 1) { n.__spaAt = performance.now(); window.__spaAdded.push(n); }
    if (f && !f.isConnected) window.__spaFormGone = true;
  });
  obs.observe(document.body, { childList: true, subtree: true });
  window.__spaFormObs = obs;
  const btn = f.querySelector('button[type=submit], input[type=submit], button:not([type])');
  if (btn) btn.click(); else f.requestSubmit();
  return true;
}"""

FORM_FEEDBACK_JS = r"""(i) => {
  window.__spaFormObs && window.__spaFormObs.disconnect();
  const f = document.forms[i];
  const shown = (e) => { if (!e.isConnected) return false; const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
    return r.width > 40 && r.height > 12 && cs.visibility !== 'hidden' && parseFloat(cs.opacity) > 0.05; };
  const said = (e) => (e.textContent || '').trim().length > 1;
  // Outermost added elements that are visible and say something. An added
  // node that is not itself visible is looked INTO: a toast library adds its
  // whole list at once, and the list is 0px tall because the toasts in it
  // are positioned (sonner's <ol>) — the visible thing is a descendant.
  const within = (e) => { if (shown(e)) return [e]; const out = [];
    const walk = (n) => { for (const c of n.children) { if (shown(c) && said(c)) out.push(c); else walk(c); } };
    if (e.isConnected) walk(e); return out; };
  const added = window.__spaAdded.flatMap(within).filter(said);
  const tops = added.filter((e) => !added.some((o) => o !== e && o.contains(e)));
  if (!tops.length) return null;
  const item = tops[tops.length - 1];
  const clean = (html) => html.replace(/\sdata-spa-[a-z-]+="[^"]*"/g, '');
  const leaves = [...item.querySelectorAll('*')].filter((e) => !e.children.length && (e.textContent || '').trim()).map((e) => e.textContent.trim());
  const inForm = f && f.isConnected && f.contains(item);
  // A validator's complaint is not a success: a field marked invalid, or a
  // message standing beside a field (its own error line).
  const beside = (e) => [e.previousElementSibling, e.nextElementSibling].some((s) => s && s.matches && s.matches('input, textarea, select'))
    || (e.parentElement && e.parentElement !== f && !!e.parentElement.querySelector(':scope > input, :scope > textarea, :scope > select'));
  if ((f && f.isConnected && f.querySelector('[aria-invalid="true"]')) || (inForm && beside(item))) {
    return { invalid: (item.textContent || '').trim().slice(0, 120) };
  }
  const gone = !f || !f.isConnected || (f.getBoundingClientRect().height < 2);
  const list = item.parentElement;
  // A toast: outside the form, announced or listed, on a layer of its own.
  let layer = false;
  for (let e = item; e && e !== document.body; e = e.parentElement) if (getComputedStyle(e).position === 'fixed') { layer = true; break; }
  const toast = !inForm && (item.matches('[role=status], [role=alert], [data-sonner-toast]') || (layer && !!list && list.matches('ol, ul')));
  item.setAttribute('data-spa-feedback-probe', '1');
  return {
    shownFor: Math.round(performance.now() - (item.__spaAt || performance.now())),
    kind: toast ? 'toast' : gone ? 'replace' : 'inline',
    html: clean(item.outerHTML),
    text: (item.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 300),
    title: leaves[0] || '',
    description: leaves[1] || '',
    list: toast && list ? clean(list.outerHTML.slice(0, list.outerHTML.indexOf('>') + 1)) + '</' + list.tagName.toLowerCase() + '>' : '',
    region: toast && list && list.parentElement && list.parentElement.getAttribute('role') === 'region'
      ? clean(list.parentElement.outerHTML.slice(0, list.parentElement.outerHTML.indexOf('>') + 1)) : '',
  };
}"""


FORM_SUCCESS = {}  # route -> record_form_success()
FORM_COUNT = {}  # a route pattern's template -> its document.forms.length
TEMPLATE_CHAINS = {}  # a group's first page -> {control path: the chain of elements above it}


def record_form_success(page, url):
    """[{form, kind, html, text, title, description, list, region, ms}] — see above."""
    out = []
    try:
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(url, wait_until="networkidle")
        count = page.evaluate("() => document.forms.length")
    except Exception as exc:  # noqa: BLE001 — never fail a capture over a probe
        warn(f"{url}: form probe could not load the page ({exc})")
        return out
    for i in range(count):
        attempted = []

        def block(route):
            if route.request.resource_type in ("fetch", "xhr", "document", "eventsource", "websocket", "ping", "beacon", "other"):
                attempted.append(route.request.url)
                return route.abort()
            return route.continue_()

        try:
            page.goto(url, wait_until="networkidle")
            settle(page, quick=True)
            fill = page.evaluate(FORM_FILL_JS, i)
            if not fill["ok"]:
                continue
            page.route("**/*", block)
            try:
                page.evaluate(FORM_WATCH_JS, i)
                # Not "until the page is quiet": an app often answers after a
                # pretend round trip (a timer before its toast), and the page
                # is quiet until then. Wait up to 4 s for something visible
                # that says something, a request, or the form going away; then
                # let it finish arriving.
                for _ in range(40):
                    page.wait_for_timeout(100)
                    if attempted or page.evaluate("""() => window.__spaFormGone || (window.__spaAdded || []).some((e) => e.isConnected
                        && [e, ...e.querySelectorAll('*')].some((n) => { const r = n.getBoundingClientRect();
                          return r.width > 40 && r.height > 12 && (n.textContent || '').trim().length > 1; }))"""):
                        break
                quiesce(page, 1500)
                page.wait_for_timeout(300)
                # A request (a navigation included) means the answer was the
                # server's: nothing to record, and the page may be gone.
                fb = None if attempted else page.evaluate(FORM_FEEDBACK_JS, i)
            finally:
                page.unroute("**/*", block)
            if attempted:
                report.setdefault("formsPosting", []).append({"url": url, "form": i, "requests": attempted[:3]})
                continue
            if not fb:
                continue
            if fb.get("invalid"):
                warn(f"{url}: form {i}: the filled submit was refused by the app's own validation "
                     f"({fb['invalid']!r}) — no success feedback recorded")
                continue
            # How long the design keeps it on screen.
            waited = 0
            while waited < 10000:
                page.wait_for_timeout(200)
                waited += 200
                if not page.evaluate("""() => { const e = document.querySelector('[data-spa-feedback-probe]');
                    if (!e || !e.isConnected) return false; const r = e.getBoundingClientRect(); return r.height > 2 && parseFloat(getComputedStyle(e).opacity) > 0.05; }"""):
                    break
            shown_for = fb.pop("shownFor", 0)
            fb["ms"] = shown_for + waited if waited < 10000 else None
            fb["form"] = i
            out.append(fb)
        except Exception as exc:  # noqa: BLE001
            warn(f"{url}: form {i} success probe failed ({exc})")
    return out


def record_scroll_state(page, url):
    """A header that swaps its own classes past a scroll offset is a design
    with two resting states, and the one a visitor meets first is the one at
    scroll 0. Find the offset by walking down to it rather than assuming a
    round number — the threshold is somebody's arbitrary constant (40px on
    the reference site) and guessing it wrong shows as a header that changes
    at the wrong moment."""
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(url, wait_until="networkidle")
    settle(page, quick=True)
    base = page.evaluate("() => window.__spa.classMap()")
    found_y, after = None, None
    for y in list(range(10, 401, 10)):
        page.evaluate("(y) => window.scrollTo(0, y)", y)
        page.wait_for_timeout(120)
        cur = page.evaluate("() => window.__spa.classMap()")
        if any(cur.get(k) != v for k, v in base.items()):
            found_y, after = y, cur
            break
    if found_y is None:
        return []

    # Refine to the exact offset. The scan above steps by 10, so it reports
    # the first STEP past the threshold, not the threshold — a source that
    # swaps at `scrollY > 40` gets recorded as 50, and the converted header
    # then changes 10px later than the original's forever. Bisecting the last
    # step costs about four evaluations and removes the approximation.
    lo, hi = found_y - 10, found_y
    while hi - lo > 1:
        mid = (lo + hi) // 2
        page.evaluate("(y) => window.scrollTo(0, y)", mid)
        page.wait_for_timeout(110)
        cur = page.evaluate("() => window.__spa.classMap()")
        if any(cur.get(k) != v for k, v in base.items()):
            hi = mid
        else:
            lo = mid
    found_y = hi
    quiesce(page, 700)  # let the class transition finish before reading
    after = page.evaluate("() => window.__spa.classMap()")

    # Store the DELTA, never the two full class strings. The elements that
    # swap on scroll are the header's — shared chrome — and a full string
    # carries whatever ELSE that class attribute held on the page being
    # recorded, which for a nav link is its active state. Verified live: the
    # same header link recorded `…text-primary-foreground opacity-80` on the
    # home page and `…text-primary-foreground opacity-100 border-b
    # border-current pb-1` on the Story page. WordPress ships ONE header
    # part, so the recording page's underline would be "restored" onto every
    # other page the moment a visitor scrolled — the active-state bleed this
    # pipeline already fights elsewhere, re-entering through a data
    # attribute. The delta cancels it: the active classes sit on BOTH sides
    # of the transition and drop out.
    records = []
    for k, v in base.items():
        a = after.get(k)
        if a is None or a == v:
            continue
        off_tokens, on_tokens = v.split(), a.split()
        off_set, on_set = set(off_tokens), set(on_tokens)
        add = [t for t in on_tokens if t not in off_set]
        remove = [t for t in off_tokens if t not in on_set]
        rec = {"path": k, "y": found_y}
        if add or remove:
            rec["add"], rec["remove"] = add, remove
        else:
            # Same tokens, different string — a pure reorder. Nothing
            # meaningful to diff, so fall back to the whole swap and say so,
            # because that fallback IS the page-contaminating form.
            rec["off"], rec["on"] = v, a
            warn(f"scroll state at {k} differs only by class ORDER — stored as a full "
                 f"class swap, which carries this page's state into shared chrome")
        records.append(rec)
    return records


# ------------------------------------------------------ form validation

# What an EMPTY submit shows. A component form validates in script — zod,
# react-hook-form, a hand-written check — and prints its messages as elements
# that exist only after the submit ("Please enter your name" under the field,
# or a toast). Captured at rest, the converted form had none of them: the
# owner's visitors pressed Send on an empty form and nothing said why.
#
# Recorded, never authored: submit each form with nothing typed, keep the
# subtrees the app ADDED (the same top-level-new-node rule as a disclosure's
# panel), and how long each stayed. A message inside the form belongs to the
# nearest control BEFORE it; one outside the form (a toast) or before every
# control belongs to the form as a whole. Only the empty state is recorded —
# submitting a filled form could send it — so a message the app shows for a
# FILLED but invalid field (a malformed email) is reported, not replayed.
FORM_COLLECT_JS = r"""
(formIndex) => {
  const form = document.forms[formIndex];
  const base = window.__spaBaseSet;
  // An empty submit that navigated (a real action= on a static page) left
  // the snapshot with the old document: this page printed nothing to record.
  if (!base) return { form: null, controls: [], added: [] };
  const value = (c) => ['INPUT', 'TEXTAREA', 'SELECT'].includes(c.tagName)
    && !['hidden', 'submit', 'button', 'reset', 'image', 'file', 'checkbox', 'radio'].includes((c.type || '').toLowerCase());
  const controls = form ? [...form.elements].filter(value) : [];
  const added = [];
  for (const el of document.querySelectorAll('body *')) {
    if (base.has(el)) continue;
    const parent = el.parentElement;
    if (!parent || !base.has(parent)) continue;
    if (!(el.textContent || '').trim()) continue;
    let prev = el.previousElementSibling;
    while (prev && !base.has(prev)) prev = prev.previousElementSibling;
    let field = null;
    if (form && form.contains(el)) {
      for (const c of controls) {
        if (c.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) field = c;
      }
    }
    const clone = el.cloneNode(true);
    window.__spa.cleanStyle(clone);
    added.push({
      parentPath: window.__spa.pathOf(parent),
      afterPath: prev ? window.__spa.pathOf(prev) : null,
      html: clone.outerHTML,
      text: (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 160),
      field: field ? window.__spa.pathOf(field) : null,
    });
  }
  return { form: form ? window.__spa.pathOf(form) : null, controls: controls.map((c) => window.__spa.pathOf(c)), added };
}
"""


FORM_RECORDS = {}


def record_form_validation(page, url):
    """One record per form whose empty submit made the app print something.
    Each form is recorded from a fresh load: one form's messages must not be
    read as another's, and a submit can leave state behind."""
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(url, wait_until="networkidle")
    settle(page, quick=True)
    count = page.evaluate("() => document.forms.length")
    out = []
    for fi in range(count):
        if fi:
            page.goto(url, wait_until="networkidle")
            settle(page, quick=True)
        page.evaluate("() => window.__spa.snapshot()")
        submit = page.locator("form").nth(fi).locator("button[type=submit], input[type=submit], button:not([type])").last
        try:
            if submit.count():
                submit.click(timeout=3000)
            else:
                page.evaluate("(i) => document.forms[i].requestSubmit()", fi)
        except Exception as exc:  # noqa: BLE001 — a form that cannot be submitted has nothing to record
            warn(f"{url}: form {fi + 1} could not be submitted empty ({exc.__class__.__name__}) — no validation recorded")
            continue
        page.wait_for_timeout(900)
        got = page.evaluate(FORM_COLLECT_JS, fi)
        if not got["form"] or not got["added"]:
            continue
        # How long each message stays: a field error stays until the field is
        # edited, a toast leaves on its own. Polled, so a toast's lifetime is
        # replayed rather than a guessed four seconds.
        texts = {a["text"] for a in got["added"]}
        gone_at = {}
        for t in range(500, 7001, 500):
            page.wait_for_timeout(500)
            now = page.evaluate("() => document.body.innerText")
            for text in texts - set(gone_at):
                if text[:60] not in now:
                    gone_at[text] = t
        for a in got["added"]:
            if a["text"] in gone_at:
                a["ttl"] = gone_at[a["text"]]
        out.append(got)
    return out


FORM_RESOLVE_JS = r"""
(forms) => {
  // Resolved before anything else is inserted, so no insertion can shift a
  // recorded path (the rule APPLY_JS follows for starts-open panels).
  window.__spaForms = forms.map((f) => ({
    form: window.__spa.elAt(f.form),
    controls: f.controls.map((p) => window.__spa.elAt(p)),
    added: f.added.map((a) => ({ ...a, parentEl: window.__spa.elAt(a.parentPath),
      afterEl: a.afterPath ? window.__spa.elAt(a.afterPath) : null,
      fieldEl: a.field ? window.__spa.elAt(a.field) : null })),
  }));
}
"""

FORM_STAMP_JS = r"""
() => {
  const notes = [];
  (window.__spaForms || []).forEach((f, fi) => {
    if (!f.form) { notes.push('form ' + (fi + 1) + ' vanished before its validation could be stamped'); return; }
    const token = 'v' + (fi + 1);
    f.form.setAttribute('data-spa-validate', token);
    f.controls.forEach((c, ci) => { if (c) c.setAttribute('data-spa-vfield', token + '-' + (ci + 1)); });
    for (const a of f.added) {
      if (!a.parentEl) { notes.push('validation message parent vanished: ' + a.text); continue; }
      const tmp = document.createElement('div');
      tmp.innerHTML = a.html;
      const node = tmp.firstElementChild;
      if (!node) continue;
      node.setAttribute('data-spa-invalid', token);
      if (a.fieldEl && a.fieldEl.getAttribute('data-spa-vfield')) node.setAttribute('data-spa-for', a.fieldEl.getAttribute('data-spa-vfield'));
      if (a.ttl) node.setAttribute('data-spa-ttl', String(a.ttl));
      if (node.getAttribute('style')) node.setAttribute('data-spa-style', node.getAttribute('style'));
      node.setAttribute('hidden', '');
      node.style.display = 'none';
      if (a.afterEl && a.afterEl.parentElement === a.parentEl) a.afterEl.insertAdjacentElement('afterend', node);
      else a.parentEl.insertBefore(node, a.parentEl.firstChild);
    }
  });
  delete window.__spaForms;
  return notes;
}
"""

# ---------------------------------------------------------------- runtime

RUNTIME = r"""/* spa-runtime.js — generated by html2wp-sub prerender-spa.py.
 *
 * Replays the state transitions recorded from the original application. It
 * knows nothing about this site: every behaviour is read from data-spa-*
 * attributes written into the markup at prerender time. Editing the markup
 * in WordPress therefore cannot desynchronise it from a script, because
 * there is no site-specific script to desynchronise from.
 */
(function () {
  'use strict';

  function parse(el, name, fallback) {
    var raw = el.getAttribute(name);
    if (!raw) return fallback;
    try { return JSON.parse(raw); } catch (e) { return fallback; }
  }

  /* Recorded data is stored CONTENT, not code.
   *
   * Every data-spa-* record lives in the page markup, and WordPress keeps
   * data-* attributes through wp_kses_post — so a record can be written by
   * anyone who can save a post, including roles WordPress never lets write a
   * script. Replayed blindly, `{"attr":"onmouseover", …}` or an inner of
   * `<img onerror=…>` would run for every visitor. So a replay only ever
   * writes what a recorder writes: state attributes, presentation, a media
   * source, a safe link — and markup is rebuilt through an inert parse, never
   * assigned as HTML. What the recorder records (class, aria-*, src on the
   * gallery image, value on a stepper, SVG icons) all passes unchanged. */
  var ATTR_NAMES = /^(class|id|hidden|value|open|disabled|checked|selected|role|tabindex|type|title|alt|lang|dir|width|height|style|d|viewbox|fill|stroke|x|y|x1|y1|x2|y2|cx|cy|r|rx|ry|points|transform|opacity|offset|mask|xmlns|preserveaspectratio)$/;
  var MEDIA_TAGS = /^(img|source|video|audio|track|picture)$/;
  var INERT_TAGS = /^(script|style|iframe|frame|frameset|object|embed|applet|base|link|meta|template|noscript|noembed|xmp|plaintext|foreignobject|animate|animatemotion|animatetransform|set|image|feimage|math|portal|param)$/;
  // Controls are never rebuilt from a record (a form there would post), but a
  // recorded field IS a target: a stepper writes its value.
  var FORM_TAGS = /^(form|input|select|option|textarea)$/;
  var KEEP_TAGS = /^(svg|g|use|path|line|polyline|polygon|circle|ellipse|rect|title|desc|defs|lineargradient|radialgradient|stop|clippath|mask|symbol|span|i|b|strong|em|small|sub|sup|br|hr|img|picture|source|abbr|kbd|mark|s|u|del|ins|code|div|p|li|ol|ul|dl|dt|dd|h1|h2|h3|h4|h5|h6|button|a|section|header|footer|aside|article|figure|figcaption|label|blockquote|time|output)$/;

  function safeUrl(v) {
    var s = String(v).replace(/[\u0000- \u007f-\u009f]/g, '').toLowerCase();
    var scheme = /^([a-z][a-z0-9+.-]*):/.exec(s);
    return !scheme || /^(https?|mailto|tel)$/.test(scheme[1]) || /^data:image\/(png|jpe?g|gif|webp|avif)[;,]/.test(s);
  }

  /** A style value minus any declaration that could reach past CSS. */
  function safeStyle(v) {
    return String(v).split(';').filter(function (d) {
      return !/expression\s*\(|javascript:|vbscript:|@import|behavior\s*:|-moz-binding|\\/i.test(d);
    }).join(';');
  }

  /** The value `name` may be set to on `el`, or null when it may not be set. */
  function safeAttr(el, name, value) {
    if (typeof name !== 'string') return null;
    var n = name.toLowerCase(), tag = (el.localName || '').toLowerCase(), v = String(value);
    if (INERT_TAGS.test(tag) || /^on/.test(n)) return null;
    if ('src' === n || 'srcset' === n) {
      if (!MEDIA_TAGS.test(tag)) return null;
      var urls = 'srcset' === n ? v.split(',').map(function (c) { return c.trim().split(/\s+/)[0]; }) : [v];
      return urls.every(safeUrl) ? v : null;
    }
    // A sprite icon points into this document only (#menu -> #close).
    if ('use' === tag && ('href' === n || 'xlink:href' === n)) return /^#[\w.:-]+$/.test(v) ? v : null;
    if ('href' === n) return ('a' === tag || 'area' === tag) && safeUrl(v) ? v : null;
    if ('style' === n) return safeStyle(v);
    // aria-*, data-*, stroke-width and every other hyphenated name: none is
    // an event handler (on…) or a URL-bearing attribute.
    if (ATTR_NAMES.test(n) || /^[a-z][a-z0-9]*(-[a-z0-9_.]+)+$/.test(n)) return v;
    return null;
  }

  function writeAttr(el, name, value) {
    if (typeof name !== 'string') return;
    if (value === null || value === undefined) {
      if (!INERT_TAGS.test((el.localName || '').toLowerCase())) el.removeAttribute(name);
      return;
    }
    var v = safeAttr(el, name, value);
    if (v !== null) el.setAttribute(name, v);
  }

  /** Recorded markup as nodes of this document: an inert parse, anything
   * active dropped, anything unknown unwrapped to its content. */
  function scrub(node) {
    var kids = Array.prototype.slice.call(node.childNodes);
    for (var i = 0; i < kids.length; i++) {
      var k = kids[i];
      if (3 === k.nodeType) continue;
      if (1 !== k.nodeType) { node.removeChild(k); continue; }
      var tag = k.localName.toLowerCase();
      if (INERT_TAGS.test(tag) || FORM_TAGS.test(tag)) { node.removeChild(k); continue; }
      scrub(k);
      if (!KEEP_TAGS.test(tag)) {
        while (k.firstChild) node.insertBefore(k.firstChild, k);
        node.removeChild(k);
        continue;
      }
      var names = Array.prototype.map.call(k.attributes, function (a) { return a.name; });
      for (var j = 0; j < names.length; j++) {
        var v = safeAttr(k, names[j], k.getAttribute(names[j]));
        if (v === null) k.removeAttribute(names[j]);
        else if (v !== k.getAttribute(names[j])) k.setAttribute(names[j], v);
      }
    }
  }

  function markup(html) {
    var frag = document.createDocumentFragment();
    if (typeof html !== 'string' || !html) return frag;
    var body = new DOMParser().parseFromString('<!doctype html><body>' + html, 'text/html').body;
    scrub(body);
    while (body.firstChild) {
      frag.appendChild(document.importNode(body.firstChild, true));
      body.removeChild(body.firstChild);
    }
    return frag;
  }

  function applyAttrs(changes, on) {
    if (!Array.isArray(changes)) return;
    for (var i = 0; i < changes.length; i++) {
      var c = changes[i];
      if (!c || typeof c.id !== 'string' || !/^[\w.:-]+$/.test(c.id)) continue;
      var target = document.querySelector('[data-spa-id="' + c.id + '"]');
      if (!target) continue;
      writeAttr(target, c.attr, on ? c.on : c.off);
    }
  }

  /* The design's own open/close animation, made runnable again.
   *
   * A headless accordion — Radix, and everything shaped like it — animates
   * its panel with CSS keyframes bound to `data-state`, running to a height
   * the LIBRARY publishes as a custom property at open time:
   *
   *   [data-state=open] { animation: accordion-down .2s ease-out }  (site CSS)
   *   @keyframes accordion-down { to { height: var(--x) } }         (site CSS)
   *   style="--x: 133.15625px"                                      (library, at runtime)
   *
   * A static capture keeps the first two and cannot keep the third, because
   * it only exists while the panel is open. The keyframe then animates to an
   * invalid height and the panel just appears — the converted FAQ opened with
   * no animation at all while the original eased it open.
   *
   * Nothing needs recording to repair it: the markup states which variable
   * feeds its height, in its own inline style —
   * `--radix-accordion-content-height: var(--radix-collapsible-content-height)`.
   * Read that declaration, measure what the library measured, publish it. The
   * animation is the site's own; only the number was missing.
   */
  var VAR_REF = /--[\w-]*(height|width)\s*:\s*var\(\s*(--[\w-]+)\s*\)/g;

  function measureNatural(el, axis) {
    var style = el.getAttribute('style');
    var wasHidden = el.hasAttribute('hidden');
    el.removeAttribute('hidden');
    // Suppress the animation being measured FOR, or this reads a box
    // mid-flight instead of at rest.
    el.style.setProperty('animation', 'none', 'important');
    el.style.setProperty('transition', 'none', 'important');
    el.style.setProperty('display', 'block', 'important');
    el.style.setProperty('height', 'auto', 'important');
    el.style.setProperty('visibility', 'hidden', 'important');
    var v = 'width' === axis ? el.scrollWidth : el.scrollHeight;
    if (null === style) { el.removeAttribute('style'); } else { el.setAttribute('style', style); }
    if (wasHidden) { el.setAttribute('hidden', ''); }
    return v;
  }

  /** Boxes between a panel and its trigger that declare such a variable. */
  function animatedBoxes(panels) {
    var out = [];
    for (var i = 0; i < panels.length; i++) {
      var el = panels[i];
      for (var hop = 0; el && hop < 4; hop++, el = el.parentElement) {
        var style = el.getAttribute && el.getAttribute('style');
        if (!style) { continue; }
        VAR_REF.lastIndex = 0;
        var m, refs = [];
        while ((m = VAR_REF.exec(style))) { refs.push({ axis: m[1], prop: m[2] }); }
        if (refs.length) { out.push({ el: el, refs: refs }); }
      }
    }
    return out;
  }

  function publishSizes(boxes) {
    for (var i = 0; i < boxes.length; i++) {
      for (var j = 0; j < boxes[i].refs.length; j++) {
        var r = boxes[i].refs[j];
        boxes[i].el.style.setProperty(r.prop, measureNatural(boxes[i].el, r.axis) + 'px');
      }
    }
  }

  /** How long the close animation needs before the panel may be hidden. */
  function animationMs(boxes) {
    var ms = 0;
    for (var i = 0; i < boxes.length; i++) {
      var each = (getComputedStyle(boxes[i].el).animationDuration || '0s').split(',');
      for (var j = 0; j < each.length; j++) {
        var d = parseFloat(each[j]) * (each[j].indexOf('ms') > -1 ? 1 : 1000);
        if (d > ms) { ms = d; }
      }
    }
    return Math.min(ms, 1000);
  }

  function setOpen(trigger, on) {
    // Recorded FIRST: the inner swap below makes the scroll pass re-apply
    // synchronously, and that pass asks which toggles are open.
    trigger.setAttribute('data-spa-open', on ? 'true' : 'false');
    var id = trigger.getAttribute('data-spa-toggle');
    var panels = document.querySelectorAll('[data-spa-panel="' + id + '"]');
    var boxes = animatedBoxes(panels);
    var attrs = parse(trigger, 'data-spa-attrs', []);
    if (!Array.isArray(attrs)) attrs = [];
    // Re-hiding is what CUTS the close animation short, so it has to wait for
    // it. Everything else about the closed state is applied immediately.
    var hiding = [];
    if (!on && boxes.length) {
      hiding = attrs.filter(function (c) { return 'hidden' === c.attr; });
      attrs = attrs.filter(function (c) { return 'hidden' !== c.attr; });
    }
    for (var i = 0; i < panels.length; i++) {
      var p = panels[i];
      if (on) {
        var s = safeStyle(p.getAttribute('data-spa-style') || '');
        if (s) p.setAttribute('style', s); else p.removeAttribute('style');
        p.removeAttribute('hidden');
      } else if (!boxes.length) {
        p.setAttribute('hidden', '');
        p.style.display = 'none';
      }
    }
    // The height must exist BEFORE data-state flips, or the keyframe begins
    // with nothing to animate towards.
    if (on && boxes.length) { publishSizes(boxes); }
    applyAttrs(attrs, on);
    if (!on && boxes.length) {
      (function (panelList, hideAttrs, wait) {
        window.setTimeout(function () {
          applyAttrs(hideAttrs, false);
          for (var k = 0; k < panelList.length; k++) {
            panelList[k].setAttribute('hidden', '');
            panelList[k].style.display = 'none';
          }
        }, wait);
      })(panels, hiding, animationMs(boxes));
    }
    var inner = parse(trigger, 'data-spa-inner', null);
    if (inner) {
      var next = markup(on ? inner.on : inner.off);
      while (trigger.firstChild) trigger.removeChild(trigger.firstChild);
      trigger.appendChild(next);
      // The rewrite may have replaced a scroll-recorded element with a fresh
      // node in its RESTING classes — and a fresh node is invisible to a
      // disconnect check, because nothing that IS bound went anywhere. So the
      // applier is told outright to rebuild its list and re-apply.
      window.dispatchEvent(new Event('spa:scroll-rebind'));
    }
    // Kept in sync even when the original never managed it. A hand-rolled
    // drawer routinely ships without aria-expanded; announcing the state is
    // an accessibility gain that costs no pixels, and the editor's smoke
    // test asserts this attribute flips.
    trigger.setAttribute('aria-expanded', on ? 'true' : 'false');
  }

  /* The class tokens every OPEN toggle holds on `el`: what its `on` class
   * adds over its `off` class (hold — scroll must not remove them) and what
   * it takes away (release — scroll must not put them back). */
  function heldClasses(el) {
    var out = { hold: {}, release: {}, any: false };
    var sid = el.getAttribute('data-spa-id');
    if (!sid) return out;
    var open = document.querySelectorAll('[data-spa-toggle][data-spa-open="true"]');
    for (var i = 0; i < open.length; i++) {
      var attrs = parse(open[i], 'data-spa-attrs', []);
      for (var j = 0; j < attrs.length; j++) {
        var c = attrs[j];
        if (c.id !== sid || 'class' !== c.attr) continue;
        var on = String(c.on || '').split(/\s+/).filter(Boolean);
        var off = String(c.off || '').split(/\s+/).filter(Boolean);
        on.forEach(function (t) { if (off.indexOf(t) < 0) out.hold[t] = true; });
        off.forEach(function (t) { if (on.indexOf(t) < 0) out.release[t] = true; });
        out.any = true;
      }
    }
    return out;
  }

  /* A quantity stepper is a COUNTER, not a toggle.
   *
   * Recorded like any other control, its "+" reads as a toggle whose only
   * effect is one number field's value going 1 -> 2, and its "-" (clicked
   * at the minimum) as 1 -> 1. Replayed as toggles, "+ + -" left the field
   * at 1 where the original showed 2: the second "+" switched the toggle
   * back off. A trigger whose ONLY recorded effect is the value of one
   * number-holding field therefore steps that field by what the click
   * changed it by. A step recorded as nothing (clamped at a bound) takes the
   * opposite sign of a sibling that steps the same field. */
  function stepOf(trigger, triggers) {
    var one = function (t) {
      var a = parse(t, 'data-spa-attrs', []);
      if (a.length !== 1 || a[0].attr !== 'value') return null;
      var off = parseFloat(a[0].off), on = parseFloat(a[0].on);
      if (!isFinite(off) || !isFinite(on)) return null;
      var el = document.querySelector('[data-spa-id="' + a[0].id + '"]');
      if (!el || el.tagName !== 'INPUT' || !/^(number|text|)$/i.test(el.getAttribute('type') || '')) return null;
      if (document.querySelector('[data-spa-panel="' + t.getAttribute('data-spa-toggle') + '"]')) return null;
      return { id: a[0].id, el: el, by: on - off };
    };
    var me = one(trigger);
    if (!me) return null;
    if (me.by) return me;
    for (var i = 0; i < triggers.length; i++) {
      if (triggers[i] === trigger) continue;
      var o = one(triggers[i]);
      if (o && o.id === me.id && o.by) { me.by = -o.by; return me; }
    }
    return null;
  }

  function step(s) {
    var v = (parseFloat(s.el.value) || 0) + s.by;
    var min = parseFloat(s.el.getAttribute('min')), max = parseFloat(s.el.getAttribute('max'));
    if (isFinite(min)) v = Math.max(min, v);
    if (isFinite(max)) v = Math.min(max, v);
    s.el.value = String(v);
    s.el.setAttribute('value', String(v));
    s.el.dispatchEvent(new Event('input', { bubbles: true }));
    s.el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function init() {
    var triggers = document.querySelectorAll('[data-spa-toggle]');
    for (var i = 0; i < triggers.length; i++) {
      (function (trigger) {
        // A trigger with a PANEL is a disclosure: it was captured open and has
        // to be closed at load, which is what setOpen(…, false) is for.
        //
        // A trigger with NO panel is a SWAP — a gallery thumbnail, a tab, a
        // colour chip — whose only effect is attributes on some other element.
        // Replaying its `off` state at load actively corrupts the page,
        // because `off` is whatever the element held when THAT trigger was
        // recorded, and by then an earlier thumbnail had already been clicked.
        // Measured on a converted shop: every product page loaded showing the
        // SECOND photograph as its main image while the thumbnail strip
        // highlighted the first — the markup was right and the runtime made it
        // wrong, on all twelve products, at every width. So leave the captured
        // markup exactly as captured and only act on a real click.
        var stepper = stepOf(trigger, triggers);
        if (stepper) {
          trigger.setAttribute('data-spa-open', 'false');
          trigger.addEventListener('click', function (ev) { ev.preventDefault(); step(stepper); });
          return;
        }
        var id = trigger.getAttribute('data-spa-toggle');
        if (trigger.hasAttribute('data-spa-starts-open')) {
          // Captured open, and open is how the original loads it.
          trigger.setAttribute('data-spa-open', 'true');
        } else if (document.querySelector('[data-spa-panel="' + id + '"]')) {
          setOpen(trigger, false);
        } else {
          trigger.setAttribute('data-spa-open', 'false');
        }
        trigger.addEventListener('click', function (ev) {
          ev.preventDefault();
          var swap = trigger.getAttribute('data-spa-swap');
          if (swap) {
            // A radio member: siblings' targets back to rest, then this one's.
            var members = document.querySelectorAll('[data-spa-swap="' + swap + '"]');
            for (var k = 0; k < members.length; k++) {
              if (members[k] === trigger) continue;
              applyAttrs(parse(members[k], 'data-spa-attrs', []), false);
              members[k].setAttribute('data-spa-open', 'false');
            }
            setOpen(trigger, true);
            return;
          }
          var on = trigger.getAttribute('data-spa-open') !== 'true';
          var group = trigger.getAttribute('data-spa-group');
          if (group && on) {
            var sibs = document.querySelectorAll('[data-spa-group="' + group + '"]');
            for (var j = 0; j < sibs.length; j++) {
              if (sibs[j] !== trigger) setOpen(sibs[j], false);
            }
          }
          setOpen(trigger, on);
        });
      })(triggers[i]);
    }

    // A same-page hash link inside an open disclosure closes it, where the
    // original was recorded doing so (a drawer's section items). The scroll
    // itself stays the browser's: nothing here prevents the default.
    document.addEventListener('click', function (ev) {
      var a = ev.target.closest && ev.target.closest('a[href]');
      if (!a) return;
      var u;
      try { u = new URL(a.getAttribute('href'), location.href); } catch (e) { return; }
      var page = function (p) { return p.replace(/\/index\.html$/, '/'); };
      if (!u.hash || u.origin !== location.origin || page(u.pathname) !== page(location.pathname)) return;
      var open = document.querySelectorAll('[data-spa-close-on-link][data-spa-open="true"]');
      for (var i = 0; i < open.length; i++) {
        var t = open[i];
        var scope = Array.prototype.slice.call(
          document.querySelectorAll('[data-spa-panel="' + t.getAttribute('data-spa-toggle') + '"]'));
        var attrs = parse(t, 'data-spa-attrs', []);
        if (!Array.isArray(attrs)) attrs = [];
        for (var j = 0; j < attrs.length; j++) {
          if (!attrs[j] || typeof attrs[j].id !== 'string' || !/^[\w.:-]+$/.test(attrs[j].id)) continue;
          var el = document.querySelector('[data-spa-id="' + attrs[j].id + '"]');
          // An element holding the trigger is the chrome around it, not a panel.
          if (el && !el.contains(t)) scope.push(el);
        }
        for (var k = 0; k < scope.length; k++) {
          if (scope[k].contains(a)) { setOpen(t, false); break; }
        }
      }
    });

    var collectScrollers = function () {
      var found = [];
      var nodes = document.querySelectorAll('[data-spa-scroll]');
      for (var k = 0; k < nodes.length; k++) {
        var spec = parse(nodes[k], 'data-spa-scroll', null);
        if (!spec) continue;
        // A record marked for the parent belongs to a chrome root the CMS
        // regenerates as a wrapper — the record rides on the first child so it
        // survives being put inside a template part, and resolves back up here.
        var target = nodes[k].getAttribute('data-spa-scroll-target') === 'parent'
          ? nodes[k].parentElement
          : nodes[k];
        if (target) found.push([target, spec]);
      }
      return found;
    };
    var scrollers = collectScrollers();
    if (scrollers.length) {
      // Per-element last state, not one shared flag: nothing guarantees two
      // recorded elements swap at the SAME offset, and a shared flag makes
      // the first element's threshold silently govern all of them.
      var state = new Array(scrollers.length);
      var onScroll = function () {
        var y = window.scrollY;
        // A disclosure toggle rewrites its own innerHTML on every open and
        // close, replacing any recorded element inside it with a fresh node —
        // the list then holds a detached element, and class changes applied
        // to it move nothing on screen. Measured: a drawer's hamburger went
        // back to its over-the-hero colour the moment the drawer closed, and
        // stayed there. When any bound element has left the document, the
        // list is rebuilt and every state forgotten, so the pass below
        // re-applies the truth to the nodes that are actually on the page.
        for (var d = 0; d < scrollers.length; d++) {
          if (!scrollers[d][0].isConnected) {
            scrollers = collectScrollers();
            state = new Array(scrollers.length);
            break;
          }
        }
        for (var n = 0; n < scrollers.length; n++) {
          var past = y > scrollers[n][1].y;
          if (past === state[n]) continue;
          state[n] = past;
          var el = scrollers[n][0], sp = scrollers[n][1];
          // An OPEN toggle that set classes on this same element outranks the
          // scroll position: the original computes something like
          // `transparent = atTop && !menuOpen`, so opening the mobile menu over
          // the hero turns the header solid whatever the scroll says. Before,
          // the toggle's own innerHTML swap asked this pass to re-apply, and
          // the at-top state stripped the solid classes the toggle had just
          // set — the header stayed transparent with the menu open.
          var held = heldClasses(el);
          if (sp.add || sp.remove) {
            // A token delta, so the element keeps every class the delta does
            // not mention — its active-nav state above all. Replacing the
            // whole attribute would overwrite that with the state of
            // whichever page this shared chrome was recorded on.
            var gone = (past ? (sp.remove || []) : (sp.add || []))
              .filter(function (t) { return !held.hold[t]; });
            var here = (past ? (sp.add || []) : (sp.remove || []))
              .filter(function (t) { return !held.release[t]; });
            if (gone.length) el.classList.remove.apply(el.classList, gone);
            if (here.length) el.classList.add.apply(el.classList, here);
          } else if (!held.any) {
            el.setAttribute('class', past ? sp.on : sp.off);
          }
        }
      };
      window.addEventListener('scroll', onScroll, { passive: true });
      // A disclosure rewrote its inner: the recorded element inside it is a
      // NEW node the disconnect check cannot see (everything bound is still
      // in the document). Rebuild from scratch and re-apply.
      window.addEventListener('spa:scroll-rebind', function () {
        scrollers = collectScrollers();
        state = new Array(scrollers.length);
        onScroll();
      });
      onScroll();
    }
  }

  /* Reveal-on-scroll (data-spa-reveal, recorded at prerender). The page CSS
   * hides a recorded element only under html.spa-reveal, which is set here,
   * so a page without this script — or the block editor — shows everything.
   * Each element plays its recorded transition once it enters the viewport. */
  function initReveals() {
    var els = document.querySelectorAll('[data-spa-reveal]');
    var root = document.documentElement;
    // The head boot may have hidden them already; a page this runtime will not
    // animate is handed back visible.
    if (!els.length || !('IntersectionObserver' in window)
        || (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches)) {
      root.classList.remove('spa-reveal');
      return;
    }
    root.setAttribute('data-spa-reveal-live', '');
    // "+<ms>" after the trigger; "+i<ms>" that many per sibling before it
    // that shares its trigger (a stagger by position). A listing may wrap
    // each item (a post template's <li>): then the items are the wrappers.
    var delayOf = function (el) {
      var at = el.getAttribute('data-spa-reveal-at') || '', p = at.split('+')[1] || '';
      if (p.charAt(0) !== 'i') return parseInt(p, 10) || 0;
      var same = function (n) {
        return n.getAttribute('data-spa-reveal-at') === at || !!n.querySelector('[data-spa-reveal-at="' + at + '"]');
      };
      for (var node = el, up = 0; node && up < 3; node = node.parentElement, up++) {
        var before = 0, row = false;
        for (var sib = node.previousElementSibling; sib; sib = sib.previousElementSibling) if (same(sib)) before++;
        for (sib = node.nextElementSibling; sib && !row; sib = sib.nextElementSibling) row = same(sib);
        if (before || row) return before * (parseInt(p.slice(1), 10) || 0);
      }
      return 0;
    };
    var show = function (el) {
      el.classList.add('spa-in');
      var done = function (ev) { if (!ev || ev.target === el) el.classList.add('spa-done'); };
      el.addEventListener('transitionend', done);
      setTimeout(done, 3500);
    };
    // One observer per recorded trigger depth (data-spa-reveal-at: how far
    // above the viewport bottom an element must come, as in the app).
    var observers = {}, waiting = [];
    var fire = function (el) {
      if (el.__spaFired) return;
      el.__spaFired = true;
      var io = observers[el.__spaAt];
      if (io) io.unobserve(el);
      var d = delayOf(el);
      if (d) setTimeout(show.bind(null, el), d);
      else show(el);
    };
    var observer = function (at) {
      if (observers[at]) return observers[at];
      var io = new IntersectionObserver(function (entries) {
        for (var i = 0; i < entries.length; i++) if (entries[i].isIntersecting) fire(entries[i].target);
      }, at ? { rootMargin: '0px 0px -' + at + 'px 0px' } : {});
      return (observers[at] = io);
    };
    // The observer only sees what it catches crossing: a jump (an anchor
    // link, a fast scroll) can carry an element from below the viewport to
    // above it between two frames, and it stayed hidden for good. Once the
    // page is scrolled past it, it plays. One the document ends before it
    // can come its recorded depth in stays held, as in the app (bruce's
    // footer at 820 px wide).
    var pending = false;
    var sweep = function () {
      pending = false;
      for (var i = waiting.length - 1; i >= 0; i--) {
        var el = waiting[i];
        if (el.__spaFired) { waiting.splice(i, 1); continue; }
        var r = el.getBoundingClientRect();
        if (r.bottom <= 0) { waiting.splice(i, 1); fire(el); }
      }
      if (!waiting.length) window.removeEventListener('scroll', onScroll);
    };
    var onScroll = function () {
      if (!pending) { pending = true; requestAnimationFrame(sweep); }
    };
    // Elements already on screen at load reveal from their recorded start too.
    // "t<ms>" is a reveal the app plays on a timer, wherever it is: it
    // starts that long after the app's motion began, which here is now.
    // "<depth>+<ms>" waits that long after coming in, as a stagger does.
    for (var i = 0; i < els.length; i++) {
      var at = els[i].getAttribute('data-spa-reveal-at') || '';
      if (at.charAt(0) === 't') setTimeout(show.bind(null, els[i]), parseInt(at.slice(1), 10) || 0);
      else {
        els[i].__spaAt = parseInt(at, 10) || 0;
        observer(els[i].__spaAt).observe(els[i]);
        waiting.push(els[i]);
      }
    }
    if (waiting.length) window.addEventListener('scroll', onScroll, { passive: true });
    document.documentElement.classList.add('spa-reveal');
  }

  if (document.readyState !== 'loading') { init(); initReveals(); }
  else document.addEventListener('DOMContentLoaded', function () { init(); initReveals(); });

  /* An empty submit says what the original said.
   *
   * data-spa-validate marks a form whose empty submit made the app print
   * messages; each message is in the markup already, hidden, marked
   * data-spa-invalid with the form's token and — when it belongs to one
   * field — data-spa-for that field. A field message shows while its field
   * still holds the value it had at load; a form message (a toast, or one
   * that sits before every field) shows while EVERY recorded field does,
   * which is the one state it was recorded in. Showing any cancels the
   * submit, exactly as the app's own handler did; editing a field hides its
   * message; a message the app removed on its own leaves after the same
   * time (data-spa-ttl). */
  var initial = new WeakMap();
  function atRest(c) { return c && String(c.value) === (initial.has(c) ? initial.get(c) : String(c.defaultValue || '')); }
  function hideMsg(m) { m.setAttribute('hidden', ''); m.style.display = 'none'; }
  function showMsg(m) {
    m.removeAttribute('hidden');
    var st = m.getAttribute('data-spa-style');
    m.setAttribute('style', safeStyle(st || ''));
    var ttl = parseInt(m.getAttribute('data-spa-ttl') || '0', 10);
    if (ttl) setTimeout(function () { hideMsg(m); }, ttl);
  }
  function bindValidation() {
    var forms = document.querySelectorAll('form[data-spa-validate]');
    for (var i = 0; i < forms.length; i++) {
      var fields = forms[i].querySelectorAll('[data-spa-vfield]');
      for (var j = 0; j < fields.length; j++) initial.set(fields[j], String(fields[j].value));
    }
  }
  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (!form || !form.getAttribute || !form.getAttribute('data-spa-validate')) return;
    var token = form.getAttribute('data-spa-validate');
    var msgs = document.querySelectorAll('[data-spa-invalid="' + token + '"]');
    if (!msgs.length) return;
    var fields = form.querySelectorAll('[data-spa-vfield]');
    var allAtRest = true;
    for (var j = 0; j < fields.length; j++) if (!atRest(fields[j])) { allAtRest = false; break; }
    var shown = 0;
    for (var k = 0; k < msgs.length; k++) {
      var m = msgs[k], forId = m.getAttribute('data-spa-for');
      var field = forId ? form.querySelector('[data-spa-vfield="' + forId + '"]') : null;
      if (forId ? atRest(field) : allAtRest) { showMsg(m); shown++; } else { hideMsg(m); }
    }
    if (shown) { ev.preventDefault(); ev.stopImmediatePropagation(); }
  }, true);
  document.addEventListener('input', function (ev) {
    var f = ev.target && ev.target.closest && ev.target.closest('[data-spa-vfield]');
    if (!f) return;
    var msgs = document.querySelectorAll('[data-spa-for="' + f.getAttribute('data-spa-vfield') + '"]');
    for (var k = 0; k < msgs.length; k++) hideMsg(msgs[k]);
  }, true);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bindValidation);
  else bindValidation();
})();
"""


# ------------------------------------------------------- apply + serialize

# The runtime's stepper test (stepOf in the runtime): one recorded change, to
# a number/text <input>'s value, both ends numeric, no panel.
COUNTER_JS = r"""(t) => {
  const a = JSON.parse(t.getAttribute('data-spa-attrs') || '[]');
  if (a.length !== 1 || a[0].attr !== 'value' || !isFinite(parseFloat(a[0].off)) || !isFinite(parseFloat(a[0].on))) return false;
  const el = document.querySelector('[data-spa-id="' + a[0].id + '"]');
  if (!el || el.tagName !== 'INPUT' || !/^(number|text|)$/i.test(el.getAttribute('type') || '')) return false;
  return !document.querySelector('[data-spa-panel="' + t.getAttribute('data-spa-toggle') + '"]');
}"""

SWAP_GROUPS_JS = r"""() => {
  const hasPanel = (t) => !!document.querySelector('[data-spa-panel="' + t.getAttribute('data-spa-toggle') + '"]');
  const swaps = [...document.querySelectorAll('[data-spa-toggle][data-spa-attrs]')]
    .filter((t) => !hasPanel(t) && !t.hasAttribute('data-spa-inner') && !t.hasAttribute('data-spa-starts-open'));
  const byParent = new Map();
  for (const t of swaps) {
    if (!t.parentElement) continue;
    if (!byParent.has(t.parentElement)) byParent.set(t.parentElement, []);
    byParent.get(t.parentElement).push(t);
  }
  let gn = 0;
  for (const [parent, members] of byParent) {
    const tag = members[0].tagName;
    if (members.some((t) => t.tagName !== tag)) continue;
    const same = [...parent.children].filter((c) => c.tagName === tag);
    const missing = same.filter((c) => !c.hasAttribute('data-spa-toggle'));
    // A group: every same-tag sibling is a trigger, or all but the one that
    // is active at rest. Anything looser is not one control.
    if (same.length < 2 || missing.length > 1 || members.length + missing.length !== same.length) continue;
    const targets = new Map();
    for (const t of members) {
      for (const c of JSON.parse(t.getAttribute('data-spa-attrs'))) targets.set(c.id + '|' + c.attr, c);
    }
    const gid = 's' + (++gn);
    for (const t of members) t.setAttribute('data-spa-swap', gid);
    if (missing.length === 1) {
      const m = missing[0];
      const changes = [];
      for (const c of targets.values()) {
        const el = document.querySelector('[data-spa-id="' + c.id + '"]');
        if (!el) continue;
        const v = el.getAttribute(c.attr);
        changes.push({ id: c.id, attr: c.attr, off: v, on: v });
      }
      if (changes.length) {
        m.setAttribute('data-spa-toggle', 'sw' + gn);
        m.setAttribute('data-spa-attrs', JSON.stringify(changes));
        m.setAttribute('data-spa-swap', gid);
      }
    }
  }
}
"""

APPLY_JS = r"""
(payload) => {
  const { records, scroll, groups } = payload;
  const notes = [];
  // Derived from the element's PATH, never from a running counter. A counter
  // numbers in record order, so the same shared-chrome element gets `e1` on
  // a page with one disclosure and `e13` on a page with nine — which makes
  // two byte-identical headers differ, and stage 3 then ships them as two
  // separate design groups with a template part each. Anything stamped into
  // shared chrome has to be page-invariant, the same rule the scroll delta
  // follows.
  const stampId = (el, path) => {
    if (!el.hasAttribute('data-spa-id')) el.setAttribute('data-spa-id', 'e' + path);
    return el.getAttribute('data-spa-id');
  };

  // Starts-open panels are elements already in the at-rest markup. Resolved
  // BEFORE any recorded panel is inserted, so an insertion into the same
  // parent cannot shift the path they were recorded at.
  const openPanels = records.map(rec => (rec.openPanels || []).map(p => window.__spa.elAt(p)));
  records.forEach((rec, ri) => {
    const trigger = window.__spa.elAt(rec.trigger);
    if (!trigger) { notes.push('trigger vanished: ' + rec.trigger); return; }
    const tid = 't' + (ri + 1);
    trigger.setAttribute('data-spa-toggle', tid);
    if (rec.group) trigger.setAttribute('data-spa-group', rec.group);
    if (rec.closeOnLink) trigger.setAttribute('data-spa-close-on-link', '1');
    if (rec.startsOpen) {
      trigger.setAttribute('data-spa-starts-open', '1');
      for (const node of openPanels[ri]) {
        if (node) {
          node.setAttribute('data-spa-panel', tid);
          // What reopening restores (setOpen writes data-spa-style back).
          if (node.getAttribute('style')) node.setAttribute('data-spa-style', node.getAttribute('style'));
        }
        else notes.push('open panel vanished for ' + (rec.label || rec.trigger));
      }
    }

    // Several panels recorded against the SAME resting neighbour (a "Load
    // more" that appends four cards after the eighth) are in document order.
    // Each inserted straight after that neighbour came out reversed — the
    // twelfth product ninth — so each goes after the one inserted before it.
    const lastAt = new Map();
    for (const p of rec.panels) {
      const parent = window.__spa.elAt(p.parentPath);
      if (!parent) { notes.push('panel parent vanished: ' + p.parentPath); continue; }
      const tmp = document.createElement('div');
      tmp.innerHTML = p.html;
      const node = tmp.firstElementChild;
      if (!node) continue;
      node.setAttribute('data-spa-panel', tid);
      if (p.style) node.setAttribute('data-spa-style', p.style);
      node.setAttribute('hidden', '');
      node.style.display = 'none';
      const slot = p.parentPath + '|' + (p.afterPath || '');
      const prior = lastAt.get(slot);
      const after = p.afterPath ? window.__spa.elAt(p.afterPath) : null;
      if (prior && prior.parentElement === parent) prior.insertAdjacentElement('afterend', node);
      else if (after && after.parentElement === parent) after.insertAdjacentElement('afterend', node);
      else parent.insertBefore(node, parent.firstChild);
      lastAt.set(slot, node);
    }

    const changes = [];
    for (const c of rec.attrChanges) {
      const el = window.__spa.elAt(c.path);
      if (!el) continue;
      changes.push({ id: stampId(el, c.path), attr: c.attr, off: c.off, on: c.on });
    }
    if (changes.length) trigger.setAttribute('data-spa-attrs', JSON.stringify(changes));
    if (rec.triggerInner) trigger.setAttribute('data-spa-inner', JSON.stringify(rec.triggerInner));
  });

  // Swap GROUPS: sibling triggers with no panel whose clicks set attributes
  // on other elements — a gallery's thumbnails, a row of tabs, colour chips.
  // They behave as radio buttons, and two things the one-at-a-time recording
  // cannot see are restored here:
  //  - the member ACTIVE at rest changes nothing when clicked, so it was never
  //    recorded; once another thumbnail had been clicked, the first photograph
  //    could not be brought back. Its record is synthesised: every target the
  //    group touches, at the value it holds at rest (which is its "on");
  //  - each member was recorded against the resting page, so its record says
  //    nothing about the targets only a SIBLING changes (thumbnail 2's own
  //    highlight, when thumbnail 3 is clicked). The runtime therefore resets
  //    the siblings' targets to rest before applying the clicked one's `on`.
  // A lone panel-less trigger (a theme switch) is not a group and keeps its
  // toggle semantics.
  (__SWAP_GROUPS__)();

  if (payload.entrance) {
    const el = window.__spa.elAt(payload.entrance.path);
    if (el) { el.setAttribute('data-spa-enter', String(payload.entrance.ms)); }
    else { notes.push('entrance target vanished: ' + payload.entrance.path); }
  }

  for (const s of scroll) {
    const el = window.__spa.elAt(s.path);
    if (!el) { notes.push('scroll target vanished: ' + s.path); continue; }
    const spec = { y: s.y };
    if (s.add || s.remove) { spec.add = s.add || []; spec.remove = s.remove || []; }
    else { spec.off = s.off; spec.on = s.on; }
    el.setAttribute('data-spa-scroll', JSON.stringify(spec));
    // A <header>/<footer> is a CHROME ROOT, and the target CMS regenerates
    // that tag itself: WordPress builds it from the template part's
    // `tagName`, carries `className` across and drops every other attribute
    // — so a scroll record living on the tag is simply gone in the converted
    // site. That is invisible to every later gate, because they all compare
    // AT REST: verified live, the swap survived in dist and vanished in
    // WordPress, leaving a permanently transparent fixed header with the
    // page scrolling underneath it.
    //
    // So mirror the record onto the first child, marked as belonging to the
    // parent. The child is INSIDE the part, so it survives; the runtime
    // applies the record to its parentElement either way. In the static page
    // both copies resolve to the same element and apply the same class
    // delta, which classList makes idempotent.
    if (el.tagName === 'HEADER' || el.tagName === 'FOOTER') {
      const kid = el.firstElementChild;
      if (kid) {
        kid.setAttribute('data-spa-scroll', JSON.stringify(spec));
        kid.setAttribute('data-spa-scroll-target', 'parent');
      }
    }
    // An element inside a swap trigger loses this record on the FIRST page
    // load: the runtime closes every disclosure at init by rewriting the
    // trigger's innerHTML from data-spa-inner, and those strings were
    // captured in the recording browser — before any of these attributes
    // existed. Measured live: a mobile drawer button's hamburger icon kept
    // its cream over-the-hero colour after the header went solid, invisible
    // on white, because its scroll record was erased at bind while every
    // sibling's survived. So the record is written into the stored string
    // too. Only the CLOSED inner needs it: opening the drawer forces the
    // solid header, and that swap already rides on the trigger's own attrs.
    const trig = el.closest('[data-spa-inner]');
    if (trig && trig !== el) {
      const rel = [];
      for (let n = el; n && n !== trig; n = n.parentElement) {
        rel.unshift(Array.prototype.indexOf.call(n.parentElement.children, n));
      }
      try {
        const stored = JSON.parse(trig.getAttribute('data-spa-inner'));
        const tmp = document.createElement('div');
        tmp.innerHTML = stored.off;
        let node = tmp;
        for (const idx of rel) { node = node.children[idx]; if (!node) break; }
        if (node && node !== tmp) {
          node.setAttribute('data-spa-scroll', JSON.stringify(spec));
          stored.off = tmp.innerHTML;
          trig.setAttribute('data-spa-inner', JSON.stringify(stored));
        } else {
          notes.push('scroll record inside a swap trigger, but its stored inner has no matching node: ' + s.path);
        }
      } catch (e) {
        notes.push('scroll record inside a swap trigger with unreadable data-spa-inner: ' + s.path);
      }
    }
  }
  return notes;
}
""".replace("__SWAP_GROUPS__", SWAP_GROUPS_JS)

# A navigating control becomes the link it always was. Same attributes, same
# children, one element swapped for another at the same index — so every
# recorded path stays valid for APPLY_JS after it. Kept only if nothing
# visible moved: the box, the text's own box, and the text styles that differ
# between <a> and <button> when a site does not reset them (UA button font,
# UA link underline and colour). Any difference and the button stays, with a
# note: a dead control is a known gap, a changed pixel is a failed gate.
LINKS_JS = r"""
(links) => {
  const notes = [];
  const sig = (el) => {
    const r = el.getBoundingClientRect();
    const g = document.createRange(); g.selectNodeContents(el);
    const t = g.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return [r.x, r.y, r.width, r.height, t.x, t.y, t.width, t.height].map(v => Math.round(v * 2) / 2)
      .concat([cs.color, cs.fontFamily, cs.fontSize, cs.fontWeight, cs.lineHeight, cs.textDecorationLine,
               cs.textAlign, cs.backgroundColor, cs.borderTopWidth, cs.paddingTop, cs.paddingLeft]).join('|');
  };
  let swapped = 0;
  for (const l of links) {
    const b = window.__spa.elAt(l.trigger);
    if (!b || b.tagName !== 'BUTTON') { notes.push('link "' + l.label + '": control not found at capture — left as it was'); continue; }
    // <Link><Button/></Link>: the anchor around it already navigates.
    if (b.closest('a[href]')) continue;
    const a = document.createElement('a');
    for (const n of b.getAttributeNames()) {
      if (n === 'type' || n === 'disabled' || n === 'value' || n === 'name' || n.startsWith('form')) continue;
      a.setAttribute(n, b.getAttribute(n));
    }
    a.setAttribute('href', l.to);
    const before = sig(b);
    while (b.firstChild) a.appendChild(b.firstChild);
    b.replaceWith(a);
    if (sig(a) !== before) {
      while (a.firstChild) b.appendChild(a.firstChild);
      a.replaceWith(b);
      notes.push('link "' + l.label + '" -> ' + l.to + ': an <a> renders differently here — left as a <button> that does nothing');
      continue;
    }
    swapped++;
  }
  return { notes, swapped };
}
"""


STRIP_AND_LINK_JS = r"""
(payload) => {
  const { routeMap, depth } = payload;
  const notes = [];

  // The framework bundle must not travel. If it re-mounts on the converted
  // page it re-renders the root from its component tree and discards
  // whatever the owner edited in WordPress.
  const dropped = [];
  for (const s of document.querySelectorAll('script[type="module"], script[src]')) {
    const src = s.getAttribute('src') || '';
    if (s.type === 'module' || /\.(m?js)(\?|$)/.test(src)) { if (src) dropped.push(src); s.remove(); }
  }
  for (const l of document.querySelectorAll('link[rel="modulepreload"], link[rel="preload"][as="script"]')) {
    const href = l.getAttribute('href') || '';
    if (href) dropped.push(href);
    l.remove();
  }

  // Internal hrefs become the flat filenames stage 0 expects. Anything not
  // in the route table is left exactly as authored and reported — a guess
  // here would silently retarget a real link.
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.getAttribute('href');
    if (!href || /^(https?:|mailto:|tel:|#|javascript:)/i.test(href)) continue;
    if (!href.startsWith('/')) continue;
    const clean = href.split(/[?#]/)[0];
    const suffix = href.slice(clean.length);
    const file = routeMap[clean] || routeMap[clean.replace(/\/$/, '')];
    if (!file) { notes.push('unmapped internal link: ' + href); continue; }
    a.setAttribute('href', depth + file + suffix);
  }

  // The runtime tag is NOT appended here. Appending a <script src> to a
  // live document makes the browser fetch it immediately — from the dist
  // server, where the file does not exist, because it is written into the
  // OUTPUT directory. That produced one spurious 404 console error per page
  // and would have read, to anyone auditing the log later, as a real broken
  // asset in the client's site. It is injected into the serialised string
  // instead, where nothing fetches anything.

  // React's SSR/hydration markers: `<!--$-->…<!--/$-->` around Suspense
  // boundaries and `<!-- -->` between adjacent text parts. They mean nothing
  // once React is gone, and they are not inert downstream: the editor
  // addresses content by position, and a save resolved on the server with
  // these comments in the tree pointed at "part of the page that is no longer
  // there" (TanStack Start, Lovable's current generator, emits them on every
  // page; a client-rendered React Router app does not). Only these exact
  // marker comments; any other comment is the author's and stays.
  {
    const MARKERS = new Set(['$', '/$', '$?', '$!', '&', '/&', '', ' ']);
    const walker = document.createTreeWalker(document.documentElement, NodeFilter.SHOW_COMMENT);
    const drop = [];
    while (walker.nextNode()) if (MARKERS.has(walker.currentNode.data)) drop.push(walker.currentNode);
    for (const c of drop) c.remove();
    document.body.normalize();
  }

  // The prerenderer's own footprint must not ship. `scroll-behavior: auto`
  // is set on <html> so the scroll-through actually reaches the bottom
  // (see settle()); leaving it in the markup writes a capture artifact into
  // the client's site — and a real one, since the original scrolls smoothly.
  const de = document.documentElement;
  de.style.removeProperty('scroll-behavior');
  if (!de.getAttribute('style')) de.removeAttribute('style');
  for (const img of document.querySelectorAll('img[loading="eager"]')) {
    // forced by settle() to defeat lazy-load; the source authored `lazy`
    if (img.__spaWasLazy) img.setAttribute('loading', 'lazy');
  }
  return { notes, dropped };
}
"""


# Every bundle path removed from a <script src>/modulepreload, across all
# pages — the files to prune from the output afterwards.
dropped_scripts = set()


def measure_entrance(page):
    """The route-level fade, timed off the running application.

    A page-transition component fades the whole route in on mount. It is the
    ONE piece of entrance motion a converted site can honestly keep: it
    belongs to the PAGE rather than to a scroll position, so a real
    navigation reproduces exactly the trigger the original had. Everything
    else (reveal-on-scroll, a hero's slow zoom) is deliberately captured at
    rest — see the stage notes.

    Measured, not assumed: find the largest element that is still transparent
    just after load, then poll until it settles opaque and keep how long that
    took. The EXIT half cannot come back — it needs a router to delay the
    navigation, and a converted site has real page loads.
    """
    FIND = """() => {
      let best = null, area = 0;
      for (const e of document.querySelectorAll('[style*="opacity"]')) {
        if (!(parseFloat(getComputedStyle(e).opacity) < 0.99)) continue;
        const r = e.getBoundingClientRect();
        if (r.width * r.height > area) { area = r.width * r.height; best = e; }
      }
      if (!best) return null;
      best.setAttribute('data-spa-enter-probe', '1');
      return { path: window.__spa.pathOf(best) };
    }"""
    # WAIT for the fade to begin before timing it. Straight after navigation
    # the framework has not mounted yet, so everything reads opaque and a
    # single probe concludes there is no entrance at all — the measurement
    # would miss precisely the animation it exists to find.
    start, waited_for_start = None, 0
    while waited_for_start < 4000:
        start = page.evaluate(FIND)
        if start:
            break
        page.wait_for_timeout(50)
        waited_for_start += 50
    if not start:
        return None
    waited = 0
    while waited < 3000:
        page.wait_for_timeout(50)
        waited += 50
        if page.evaluate("""() => { const e = document.querySelector('[data-spa-enter-probe]');
                                    return e ? parseFloat(getComputedStyle(e).opacity) : 1; }""") >= 0.99:
            break
    page.evaluate("""() => { const e = document.querySelector('[data-spa-enter-probe]');
                             if (e) e.removeAttribute('data-spa-enter-probe'); }""")
    return {"path": start["path"], "ms": max(120, waited)}


# Reveal-on-scroll, recorded. Elements the running app holds hidden at load
# (opacity under 0.5 on the element itself — a whileInView/IntersectionObserver
# reveal before it fires) and that are visible once the page has been scrolled
# through: their starting look and the time the reveal took become
# data-spa-reveal. The page CSS (reveal_css) hides them ONLY under the root
# class spa-runtime.js adds, and the runtime shows each one as it enters the
# viewport; without the script, or with reduced motion, content is simply
# visible. Watching runs during settle()'s own scroll-through.
REVEAL_WATCH_JS = """() => {
  const found = [];
  for (const e of document.body.querySelectorAll('*')) {
    const cs = getComputedStyle(e);
    if (!(parseFloat(cs.opacity) < 0.5) || cs.display === 'none' || cs.visibility === 'hidden') continue;
    if (e.closest('[hidden],[aria-hidden="true"]')) continue;
    const r = e.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    if (found.some(f => f.contains(e))) continue;
    e.__spaReveal = { o: parseFloat(cs.opacity), t: cs.transform, seen: 0, done: 0, top0: r.top, scrollSeen: null };
    // Its component: the look it starts from, on the same kind of element.
    // Read now, before a reveal can change its classes.
    const s = e.__spaReveal;
    s.look = s.o + '|' + s.t;
    s.comp = s.look + '|' + e.tagName + '.' + (e.getAttribute('class') || '');
    found.push(e);
  }
  window.__spaReveals = found;
  const t0 = performance.now();
  // Until it starts, each element's depth into the viewport is kept as it
  // changes: once the trigger depth is known, that says when the app's
  // trigger fired, and the start after it is the reveal's own delay.
  window.__spaRevealTimer = setInterval(() => {
    const at = performance.now(), now = at - t0;
    for (const e of found) {
      const s = e.__spaReveal, op = parseFloat(getComputedStyle(e).opacity);
      if (!s.seen) {
        const r = e.getBoundingClientRect(), depth = innerHeight - r.top;
        const h = s.hist || (s.hist = []);
        if (!h.length || Math.abs(h[h.length - 1][1] - depth) > 0.5) h.push([at, depth, r.bottom]);
      }
      if (!s.seen && op > s.o + 0.02) { s.seen = now; s.seenAt = at; s.scrollSeen = window.scrollY; }
      if (s.seen && !s.done && op >= 0.99) s.done = now;
    }
  }, 16);
  return found.length;
}"""

# REVEAL_WATCH_JS, installed at the app's mount rather than whenever the
# recorder next looks. A client-rendered app mounts and starts its load
# motion within a frame or two, before any poll from here could see its
# first state: elements already mid-fade are recorded from a wrong starting
# look, and a stagger they started together reads as simultaneous. So the
# watch goes in from an init script, in the mutation callback (a microtask,
# before the next frame) that finds the app's content — or at
# DOMContentLoaded for a page whose content arrived with its HTML. Armed
# per load by a one-shot sessionStorage flag, so only the capture pays.
REVEAL_WATCH_BOOT = """(() => {
  let on = false;
  try { on = sessionStorage.getItem('__spaWatch') === '1'; if (on) sessionStorage.removeItem('__spaWatch'); } catch (e) {}
  if (!on) return;
  const watch = %s;
  let done = false;
  const ready = () => !!document.body && document.body.querySelectorAll('*').length > 20;
  const go = () => { if (done) return; done = true; mo.disconnect(); window.__spaWatched = watch(); };
  const mo = new MutationObserver(() => { if (document.readyState !== 'loading' && ready()) go(); });
  mo.observe(document, { childList: true, subtree: true });
  // Content that came with the HTML (a server-rendered or static page) is
  // there when parsing ends, however little of it there is.
  document.addEventListener('DOMContentLoaded', () => { if (ready() || (document.body && document.body.innerText.trim())) go(); });
})();""" % REVEAL_WATCH_JS


# Run between the watch and settle()'s scroll-through, at scroll 0.
# First it lets the load-time motion finish: an element below the fold that
# still reveals without any scroll is on a timer (a hero's staggered
# entrance), not on the viewport, so it is replayed on the same timer.
# Then it measures how far into the viewport an element must come before the
# app reveals it (the IntersectionObserver / framer viewport margin): the
# first unrevealed element is brought in a step at a time, coarsely, and the
# next one finely within the last coarse steps. One margin per starting look
# (a page can mix components with different margins); a reveal whose look
# was not measured takes the page's first.
REVEAL_PROBE_JS = """async () => {
  const found = window.__spaReveals || [];
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const frames = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  window.__spaRevealAt = 0;
  if (!found.length) return 0;
  document.documentElement.style.scrollBehavior = 'auto';
  let last = -1, still = 0;
  for (let t = 0; t < 3000 && still < 600; t += 100) {
    const n = found.filter((e) => e.__spaReveal && e.__spaReveal.seen).length;
    still = n === last ? still + 100 : 0; last = n;
    await wait(100);
  }
  const H = innerHeight;
  for (const e of found) {
    const s = e.__spaReveal;
    if (s && s.seen && s.scrollSeen === 0 && s.top0 >= H) s.timed = true;
  }
  // From here on the probe scrolls: a reveal seen at scroll 0 after this
  // (back at the top) did not play at load.
  window.__spaRevealScrolled = performance.now();
  const maxY = document.documentElement.scrollHeight - H;
  const cands = found.filter((e) => e.isConnected && e.__spaReveal && !e.__spaReveal.seen)
    .map((e) => [e, e.getBoundingClientRect().top + scrollY]).sort((a, b) => a[1] - b[1]);
  // Started, not just visible: an eased fade barely moves in its first
  // frames, and a staggered one sits out its delay first — waiting for
  // either would overshoot the depth by a step or more. A new animation on
  // the element (a transition or WAAPI animation still in its delay) counts.
  const anims = (e) => (e.getAnimations ? e.getAnimations().length : 0);
  const started = (e, base) => {
    const s = e.__spaReveal, cs = getComputedStyle(e);
    return s.seen || parseFloat(cs.opacity) > s.o + 0.001 || cs.transform !== s.t || anims(e) > base;
  };
  // Depth at which `e` reveals, trying from..to in `step`s; 0 if it never does.
  // `hold` is the wait per step: short for the coarse pass, long enough in
  // the fine pass to sit out a small stagger delay before the start shows.
  // Every step is also evidence about the other waiting reveals: the
  // deepest each got without starting, and how deep it was once it had.
  for (const [e] of cands) { e.__spaReveal.a0 = anims(e); e.__spaReveal.lb = 0; }
  const note = () => {
    for (const [e] of cands) {
      const s = e.__spaReveal;
      if (s.ub !== undefined) continue;
      const r = e.getBoundingClientRect(), depth = H - r.top;
      // Evidence only while its top is on screen: one carried past it, or
      // only an edge of it left in view, says nothing about the margin.
      if (started(e, s.a0)) s.ub = depth; else if (r.top >= 0 && depth > 0) s.lb = Math.max(s.lb, depth);
    }
  };
  // With `mid`, the depth returned is the middle of what the steps leave
  // open (it did not start one step shallower), not the step it started at.
  const probe = async (e, top, from, to, step, hold, mid) => {
    const d0 = Math.max(from, Math.ceil(H - top) + 1);
    for (let d = d0; d <= to; d += step) {
      if (top - H + d > maxY) return 0;
      window.scrollTo(0, Math.max(0, top - H + d));
      await frames(); await wait(hold);
      note();
      if (e.__spaReveal.ub !== undefined) return mid && d > d0 ? d - (step - 1) / 2 : d;
    }
    return 0;
  };
  // Components on one page can use different margins; reveals that start
  // from the same look on the same kind of element are one component, so
  // each is measured apart, in page order. (Two components can share a look
  // and still differ: a card row held for less than the headings above it.)
  const groups = new Map();
  for (const c of cands) {
    const k = c[0].__spaReveal.comp;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(c);
  }
  const by = {};
  let first = null;
  const t0 = performance.now();
  // Depth `d` for `e` (top `top` at load), held long enough for a small
  // stagger delay to show; whether it started there.
  const at1 = async (e, top, d) => {
    window.scrollTo(0, Math.max(0, top - H + d));
    await frames(); await wait(110);
    note();
    return e.__spaReveal.ub !== undefined;
  };
  const measuredLook = {};
  for (const [k, list] of groups) {
    if (performance.now() - t0 > 15000) break;
    // One an earlier walk already brought in has no fresh depth to walk.
    // One no walk has brought in yet goes first: one that came in with
    // another (a row) may be triggered already, its start still in a delay.
    const fresh = () => open.filter((c) => !(c[0].__spaReveal.lb > 0)).concat(open.filter((c) => c[0].__spaReveal.lb > 0));
    let open = list.filter((c) => !c[0].__spaReveal.seen && c[0].__spaReveal.ub === undefined);
    open = fresh();
    // Most components that share a look with one measured share its margin
    // too: two steps either side of it say so, and spare a walk.
    const look = list[0][0].__spaReveal.look, guess = measuredLook[look];
    if (guess !== undefined && open.length) {
      const [e, top] = open[0];
      if (Math.ceil(H - top) + 1 <= guess - 8 && top - H + guess + 8 <= maxY) {
        // Once shown, a reveal cannot be walked again: one with no margin of
        // its own (the observer's default) would start at the first check
        // and leave nothing to measure. A shallow look first says so.
        if (guess > 16 && await at1(e, top, 6)) { by[k] = 0; continue; }
        const early = await at1(e, top, Math.max(1, guess - 8));
        if (!early && await at1(e, top, guess + 8)) { by[k] = guess; continue; }
        open = open.slice(1).filter((c) => !c[0].__spaReveal.seen && c[0].__spaReveal.ub === undefined);
        open = fresh();
      }
    }
    // A component with a single fresh reveal, or a single fresh row of them
    // (the rest came in together), has nothing to refine on: walk it finely.
    const rows = new Set(open.filter((c) => !(c[0].__spaReveal.lb > 0)).map((c) => Math.round(c[1])));
    if (open.length === 1 || (open.length && rows.size <= 1)) {
      const d = await probe(open[0][0], open[0][1], 1, Math.min(H / 2, 320), 3, 90, true);
      if (d) { by[k] = Math.round(d - 1); if (first === null) first = by[k]; if (measuredLook[look] === undefined) measuredLook[look] = by[k]; }
      continue;
    }
    let i = 0, coarse = 0;
    for (; i < open.length && i < 3 && !coarse; i++) coarse = await probe(open[i][0], open[i][1], 1, H / 2, 24, 30);
    if (!coarse) continue;
    let at = coarse - 1;
    for (let tries = 0; i < open.length && tries < 3; i++) {
      const [e, top] = open[i];
      // Only one no walk has brought as deep as the fine pass starts: one
      // that came in along with the first (a row, a stagger still in its
      // delay) was triggered already and would read as revealing at once.
      const s = e.__spaReveal, lo = Math.max(1, coarse - 36);
      if (s.seen || s.ub !== undefined || s.lb >= lo || Math.ceil(H - top) + 1 >= coarse) continue;
      tries++;
      const fine = await probe(e, top, Math.max(1, coarse - 36), coarse, 3, 110, true);
      if (fine) { at = Math.min(at, fine - 1); break; }
    }
    by[k] = Math.round(at);
    if (first === null) first = by[k];
    if (measuredLook[look] === undefined) measuredLook[look] = by[k];
  }
  // A component the walks never measured takes the first measured one of
  // its look, held to what the walks saw of its own reveals along the way:
  // the deepest one came without starting, and where each did start. When
  // those two are close, they are the measurement.
  const byLook = {};
  for (const [k, list] of groups) {
    const look = list[0][0].__spaReveal.look;
    if (by[k] !== undefined && byLook[look] === undefined) byLook[look] = by[k];
  }
  for (const [k, list] of groups) {
    if (by[k] !== undefined) continue;
    const ss = list.map((c) => c[0].__spaReveal);
    const lb = Math.max(0, ...ss.map((s) => s.lb || 0));
    const ubs = ss.filter((s) => s.ub !== undefined && s.ub > 0).map((s) => s.ub);
    const ub = ubs.length ? Math.min(...ubs) : Infinity;
    const look = ss[0].look;
    if (!lb && ub === Infinity) continue;
    if (lb && ub - lb <= 30) by[k] = Math.round((lb + ub) / 2);
    else {
      const guess = byLook[look] !== undefined ? byLook[look] : first !== null ? first : lb;
      by[k] = Math.round(Math.min(Math.max(guess, lb), ub === Infinity ? guess : ub - 1));
    }
  }
  window.__spaRevealAtBy = by;
  window.__spaRevealAtByLook = byLook;
  const at = first || 0;
  window.scrollTo(0, 0);
  await frames();
  return (window.__spaRevealAt = Math.round(at));
}"""

# A reveal the app played at load while on screen is ambiguous: it may be on
# a timer, or on the viewport. At another width it can sit below the fold,
# and there the two differ. These are the ones to settle, as paths.
REVEAL_AMBIGUOUS_JS = """(line) => (window.__spaReveals || []).filter((e) => {
  const s = e.__spaReveal;
  return s && s.seen && s.scrollSeen === 0 && s.seenAt < (window.__spaRevealScrolled || Infinity) && !s.timed && s.top0 >= line && s.top0 < innerHeight;
}).map((e) => window.__spa.pathOf(e))"""

# The same route loaded on a viewport too short to hold them: what still
# reveals with no scroll, below that fold, is on a timer; what stays hidden
# there waits for the viewport.
REVEAL_TIMED_JS = """async () => {
  const found = window.__spaReveals || [];
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  let last = -1, still = 0;
  for (let t = 0; t < 3000 && still < 600; t += 100) {
    const n = found.filter((e) => e.__spaReveal.seen).length;
    still = n === last ? still + 100 : 0; last = n;
    await wait(100);
  }
  const below = found.filter((e) => e.__spaReveal.top0 >= innerHeight);
  return {
    timed: below.filter((e) => e.__spaReveal.seen && e.__spaReveal.scrollSeen === 0).map((e) => window.__spa.pathOf(e)),
    held: below.filter((e) => !e.__spaReveal.seen).map((e) => window.__spa.pathOf(e)),
  };
}"""

REVEAL_SHORT_VIEWPORT = 200


def timed_reveals(page, url, paths):
    """Of `paths`, the ones the app reveals on a timer and the ones it holds
    for the viewport; a path it could not tell is in neither."""
    probe = page.context.new_page()
    try:
        probe.set_viewport_size({"width": page.viewport_size["width"], "height": REVEAL_SHORT_VIEWPORT})
        probe.goto(url, wait_until="commit")
        # Watch from the moment the elements in question are mounted.
        for _ in range(160):
            if probe.evaluate("(ps) => !!document.body && ps.every((p) => { try { return !!window.__spa.elAt(p); } catch (e) { return false; } })", paths):
                break
            probe.wait_for_timeout(25)
        probe.evaluate(REVEAL_WATCH_JS)
        probe.wait_for_load_state("networkidle")
        seen = probe.evaluate(REVEAL_TIMED_JS)
    except Exception as exc:  # noqa: BLE001 — a failed check keeps the viewport trigger
        warn(f"{url}: could not tell timed reveals from scroll reveals ({exc})")
        seen = {"timed": [], "held": []}
    finally:
        probe.close()
    return [p for p in paths if p in set(seen["timed"])], [p for p in paths if p in set(seen["held"])]


REVEAL_COLLECT_JS = """(entrancePath) => {
  clearInterval(window.__spaRevealTimer);
  // The route fade is the page entrance (data-spa-enter), not a reveal.
  const entrance = entrancePath ? window.__spa.elAt(entrancePath) : null;
  const by = window.__spaRevealAtBy || {}, byLook = window.__spaRevealAtByLook || {}, dflt = window.__spaRevealAt || 0;
  // The app's motion starts when it mounts, not when the page was requested:
  // its first reveal at load marks that moment, and every recorded time is
  // an offset from it. A converted page's mount is the runtime's start, and
  // a host that serves slower than the prerender did must not lose them.
  const scrolled = window.__spaRevealScrolled || Infinity;
  const all = (window.__spaReveals || []).map((e) => e.__spaReveal).filter(Boolean);
  const atLoadOf = (s) => s.seen && s.scrollSeen === 0 && s.seenAt < scrolled;
  // An app busy at mount paints its first frames late, with its load motion
  // already under way: what is first seen then is partly played, a stagger
  // looks simultaneous and a fade looks short. A component's reveals share
  // a duration — the longest any of them was watched playing — and each one
  // started that long before it finished.
  const dur = {};
  for (const s of all) if (s.seen && s.done) dur[s.comp] = Math.max(dur[s.comp] || 0, s.done - s.seen);
  for (const s of all) {
    s.start = s.seenAt;
    if (s.seen && s.done && atLoadOf(s)) s.start = Math.min(s.seenAt, s.seenAt - s.seen + s.done - dur[s.comp]);
  }
  const atLoad = all.filter(atLoadOf).map((s) => s.start);
  const base = atLoad.length ? Math.min(...atLoad) : 0;
  // A late first frame can put those starts before the watch began, where
  // no trigger can be told apart; the gaps between one component's starts
  // still are its stagger.
  const first = {};
  for (const s of all) if (atLoadOf(s)) first[s.comp] = Math.min(first[s.comp] === undefined ? Infinity : first[s.comp], s.start);
  // How long after its trigger (the mount, or coming `at` deep) it started:
  // a stagger's delay. Under 40 ms is the sampling, not the app.
  const delayOf = (s, at) => {
    // In the viewport, too: one a jump carried straight past never crossed.
    // One that started short of that depth has no delay to tell; one that
    // was in at load counts from the mount.
    const crossed = (s.hist || []).find((h) => h[1] > at && h[2] > 0);
    if (!crossed) return 0;
    // Less one sample: the start is seen a sample after it happens.
    let d = Math.round((s.start - Math.max(crossed[0], base) - 16) / 10) * 10;
    if (atLoadOf(s)) d = Math.max(d, Math.round((s.start - first[s.comp]) / 10) * 10);
    return d >= 40 ? Math.min(d, 3000) : 0;
  };
  const out = [];
  for (const e of window.__spaReveals || []) {
    const s = e.__spaReveal;
    if (!e.isConnected || parseFloat(getComputedStyle(e).opacity) < 0.99) continue;
    if (entrance && (e === entrance || e.contains(entrance))) continue;
    const ms = Math.max(150, Math.min(3000, Math.round(s.done && s.seen ? dur[s.comp] + 30 : 600)));
    const from = { o: Math.round(s.o * 100) / 100, t: s.t && s.t !== 'none' ? s.t : 'none', ms: Math.round(ms / 50) * 50 };
    const key = 'r' + (from.o * 100) + '-' + from.ms + '-' + (from.t === 'none' ? 'n' : Array.from(from.t).reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7).toString(36));
    e.setAttribute('data-spa-reveal', key);
    if (s.timed) e.setAttribute('data-spa-reveal-at', 't' + Math.max(0, Math.round(s.start - base)));
    else {
      let at = by[s.comp] !== undefined ? by[s.comp] : byLook[s.look] !== undefined ? byLook[s.look] : dflt;
      // One the app revealed at load, on screen, came in less deep than that.
      if (atLoadOf(s) && s.top0 >= 0 && s.top0 < innerHeight && at >= innerHeight - s.top0) at = 0;
      // "<depth>+<delay>": an older runtime reads the depth and plays at once.
      const d = s.seenAt ? delayOf(s, at) : 0;
      if (at || d) e.setAttribute('data-spa-reveal-at', String(at) + (d ? '+' + d : ''));
      e.__spaDelay = { at, d, comp: s.comp };
    }
    out.push({ key, ...from });
    delete e.__spaReveal;
  }
  // A row of siblings from one component whose delays climb by one step
  // each is a stagger by position ("<depth>+i<step>"): kept that way it
  // survives a listing that repeats one card for every post, where each
  // card's own delay would be the first card's.
  const runs = new Map();
  for (const e of window.__spaReveals || []) {
    if (!e.__spaDelay || !e.parentElement) continue;
    const k = e.__spaDelay.comp + '|' + e.__spaDelay.at;
    if (!runs.has(e.parentElement)) runs.set(e.parentElement, new Map());
    const m = runs.get(e.parentElement);
    if (!m.has(k)) m.set(k, []);
    m.get(k).push(e);
  }
  for (const m of runs.values()) for (const list of m.values()) {
    const kids = list.filter((e) => e.__spaDelay);
    // Sibling order, and only when no other reveal of the kind sits between.
    const sib = [...kids[0].parentElement.children].filter((c) => kids.includes(c));
    if (sib.length < 2) continue;
    const ds = sib.map((e) => e.__spaDelay.d);
    // The step that fits them best; each delay is sampled to about 30 ms.
    const step = Math.round(ds.reduce((a, d, i) => a + i * d, 0) / ds.reduce((a, d, i) => a + i * i, 0) / 10) * 10;
    if (step < 30 || ds[0] > 30 || ds.some((d, i) => Math.abs(d - i * step) > 40)) continue;
    for (const e of sib) e.setAttribute('data-spa-reveal-at', String(e.__spaDelay.at) + '+i' + step);
  }
  for (const e of window.__spaReveals || []) delete e.__spaDelay;
  window.__spaReveals = [];
  return out;
}"""


def reveal_css(reveals):
    """The page's reveal rules, one per distinct recorded starting look."""
    rules, seen = [], set()
    for r in reveals:
        if r["key"] in seen:
            continue
        seen.add(r["key"])
        sel = '[data-spa-reveal="%s"]' % r["key"]
        hide = "opacity:%s!important" % r["o"] + (";transform:%s!important" % r["t"] if r["t"] != "none" else "")
        # The reveal's transition applies only while it plays (from spa-in to
        # spa-done): the element's own transitions (hover) hold before and after.
        rules.append("html.spa-reveal %s.spa-in:not(.spa-done){transition:opacity %dms ease-out,transform %dms ease-out}"
                     "html.spa-reveal %s:not(.spa-in){%s}" % (sel, r["ms"], r["ms"], sel, hide))
    return ("<style data-spa-reveals>" + "".join(rules) +
            "@media (prefers-reduced-motion:reduce){html.spa-reveal [data-spa-reveal]{transition:none}}</style>"
            "<script data-spa-reveals>" + REVEAL_BOOT + "</script>")


# A reveal's duration belongs to the ELEMENT, not to the page it was measured
# on, like close-on-link. It is measured on the live page as the longest any
# of a component's reveals was watched playing, and a route whose first
# frames came late under load watches it partly played: the shared header
# measured 400 ms on every page and 350 on one, which made that page's header
# a design group of its own. So one element (same position, tag and classes,
# same starting look) seen on several routes takes one duration on all of
# them: the one most routes measured, the longest of a tie. Only the duration:
# where it starts (data-spa-reveal-at) is the page's own — a footer already on
# screen on a short page starts at once.
REVEAL_PAGES = {}
REVEAL_ORDER_JS = """() => [...document.querySelectorAll('[data-spa-reveal]')].map((e) => ({
  id: window.__spa.pathOf(e) + '|' + e.tagName + '|' + (e.getAttribute('class') || ''),
  key: e.getAttribute('data-spa-reveal'),
}))"""
REVEAL_KEY = re.compile(r"^r([^-]+)-(\d+)-(.+)$")
REVEAL_ATTR = re.compile(r'(?<=\s)data-spa-reveal="([^"]*)"')
REVEAL_BLOCK = re.compile(r"<style data-spa-reveals>.*?</style><script data-spa-reveals>.*?</script>", re.S)


def unify_reveal_durations(pages):
    """Rewrite the written pages so each element's reveal has one duration on
    every route; returns what was unified, per element."""
    measured = {}
    for pg in pages.values():
        for e in pg["order"]:
            m = REVEAL_KEY.match(e["key"] or "")
            if m:
                measured.setdefault((e["id"], m[1], m[3]), []).append(int(m[2]))
    chosen = {}
    for k, ms in measured.items():
        if len(set(ms)) > 1:
            n = {v: ms.count(v) for v in ms}
            chosen[k] = max(n, key=lambda v: (n[v], v))
    for route, pg in pages.items():
        old = [e["key"] for e in pg["order"]]
        new = []
        for e in pg["order"]:
            m = REVEAL_KEY.match(e["key"] or "")
            ms = chosen.get((e["id"], m[1], m[3])) if m else None
            new.append("r%s-%d-%s" % (m[1], ms, m[3]) if ms is not None else e["key"])
        if new == old:
            continue
        html = pg["file"].read_text()
        if REVEAL_ATTR.findall(html) != old or not REVEAL_BLOCK.search(html):
            warn(f"{route}: its reveals changed after they were listed — durations left as measured on this page")
            continue
        slots = iter(new)
        html = REVEAL_ATTR.sub(lambda _: 'data-spa-reveal="%s"' % next(slots), html)
        by_key = {r["key"]: r for r in pg["reveals"]}
        rules = [dict(by_key[o], key=n, ms=int(REVEAL_KEY.match(n)[2])) for o, n in zip(old, new) if o in by_key]
        block = reveal_css(rules)
        html = REVEAL_BLOCK.sub(lambda _: block, html, count=1)
        pg["file"].write_text(html)
    return [{"element": k[0], "ms": v, "measured": sorted(measured[k])} for k, v in chosen.items()]


# The root class BEFORE first paint. Added by the deferred runtime alone, it
# came after the page had painted: everything in the first screen showed,
# vanished, and faded back in — a flash where the original only faded in.
# This runs in the head, only where the runtime will be able to play the
# reveal (an IntersectionObserver, no reduced-motion preference), and hands
# the page back to fully visible if the runtime never starts: a missing or
# failed script must never leave content hidden.
REVEAL_BOOT = ("(function(d){try{if(!('IntersectionObserver'in window)||(window.matchMedia&&"
               "matchMedia('(prefers-reduced-motion: reduce)').matches))return;d.classList.add('spa-reveal');"
               "setTimeout(function(){if(!d.hasAttribute('data-spa-reveal-live'))d.classList.remove('spa-reveal')},4000)}"
               "catch(e){}})(document.documentElement)")


INHERIT_CHECK_JS = r"""
({records, links, scroll, forms, chains}) => {
  // The same KIND of control at the same place as on the group's first
  // page — its element and the chain of elements above it — whatever it
  // says: a size group matches a size group whatever its sizes.
  const chain = (el) => {
    const out = [];
    for (let e = el; e && e !== document.documentElement; e = e.parentElement) out.unshift(e.tagName.toLowerCase());
    return out.join('>');
  };
  const label = (e) => (e.getAttribute('aria-label') || e.textContent || '').trim().slice(0, 60);
  const same = (path, want) => {
    const e = window.__spa.elAt(path);
    if (!e) return false;
    return chains && chains[path] ? chain(e) === chains[path] : label(e) === want;
  };
  const at = (path) => !!window.__spa.elAt(path);
  const keep = (list, ok) => list.map((x, i) => (ok(x) ? i : -1)).filter((i) => i >= 0);
  return {
    records: keep(records, (r) => same(r.trigger, r.label)
      && (r.panels || []).every((p) => at(p.parentPath) && (!p.afterPath || at(p.afterPath)))
      && (r.openPanels || []).every(at)),
    links: keep(links, (l) => same(l.trigger, l.label)),
    scroll: keep(scroll, (x) => !x.path || at(x.path)),
    forms: keep(forms, (f) => {
      if (!f.form) return true;
      const form = window.__spa.elAt(f.form);
      return !!form && form.tagName === 'FORM' && f.controls.every(at) && f.added.every((a) => at(a.parentPath));
    }),
    formCount: document.forms.length,
  };
}
"""


CHAIN_JS = r"""(p) => {
  const el = window.__spa.elAt(p);
  if (!el) return null;
  const out = [];
  for (let e = el; e && e !== document.documentElement; e = e.parentElement) out.unshift(e.tagName.toLowerCase());
  return out.join('>');
}"""


def refresh_inherited(page, url, records, chains):
    """The content a group's first page's disclosures open ON THIS PAGE.

    A product's "Details" opens that product's details: replaying the first
    page's panel on every other page would show its words everywhere. So each
    such control — in the page, not the chrome, which is the same everywhere
    — is clicked once here, where it sits at the same place, and what opened
    is what gets replayed; it is closed again, and a page that did not come
    back to rest is loaded afresh before the next one. One that opens nothing
    here, or is not here, is dropped (the caller counts it). The chrome and
    the rest keep the first page's records, checked where they sit at capture."""
    todo = [i for i, r in enumerate(records) if not r.get("chrome") and r.get("panels") and not r.get("startsOpen")]
    if not todo:
        return records, 0
    out, loaded = list(records), None
    for i in todo:
        r = records[i]
        out[i] = None
        try:
            if loaded != r["width"]:
                page.set_viewport_size({"width": r["width"], "height": 900})
                page.goto(url, wait_until="networkidle")
                settle(page, quick=True)
                loaded = r["width"]
            if chains.get(r["trigger"]) and page.evaluate(CHAIN_JS, r["trigger"]) != chains[r["trigger"]]:
                continue
            settle_scroll(page)
            page.evaluate("() => window.__spa.snapshot()")
            el = page.evaluate_handle("(p) => window.__spa.elAt(p)", r["trigger"]).as_element()
            if el is None or not el.is_visible():
                continue
            before = el.evaluate("e => e.innerHTML")
            label = el.evaluate("e => (e.getAttribute('aria-label') || e.textContent || '').trim().slice(0, 60)")
            el.click(timeout=2500)
            quiesce(page, 700)
            settle_scroll(page)
            d = page.evaluate("(p) => window.__spa.diff(p)", r["trigger"])
            if d["panels"]:
                out[i] = {**r, "label": label, "panels": d["panels"],
                          "attrChanges": [a for a in d["attrChanges"] if a["attr"] != "style"],
                          "triggerInner": ({"off": before, "on": d["triggerInnerOn"]}
                                           if d["triggerInnerOn"] is not None else None)}
            try:
                el.click(timeout=2500)
                quiesce(page, 500)
            except Exception:  # noqa: BLE001
                pass
            if not page.evaluate("() => document.querySelectorAll('*').length === "
                                 "window.__spaBase.filter(r => r[0].isConnected).length"):
                loaded = None
        except Exception:  # noqa: BLE001 — a control that cannot be driven here is dropped, never the capture
            loaded = None
    kept = [r for r in out if r is not None]
    return kept, len(records) - len(kept)


def capture(page, base_url, route, routemap, has_runtime, records, scroll, links, out_file, inherited=None):
    refreshed_dropped = 0
    if inherited:
        records, refreshed_dropped = refresh_inherited(page, base_url + route, records, inherited.get("chains") or {})
    page.set_viewport_size({"width": 1440, "height": 900})
    # Forget everything the RECORDER did.
    #
    # Recording drives the page: it clicks every trigger it can find, and on a
    # shop that includes "Add to cart". The click writes to localStorage, the
    # basket survives every later navigation in this same context, and each
    # captured page then ships a header badge reading however many products the
    # recorder bought. Measured: twelve. It looks like design — a small number
    # in a coloured dot, in the right place, at the right size — so nothing
    # downstream questions it, and every visitor to the converted site would
    # have seen a basket they never filled.
    #
    # Cleared before the capture navigation rather than after, so the page
    # renders from a pristine state. Wrapped because a browser can refuse
    # storage access on an about:blank-ish origin, and losing the capture over
    # a storage exception would be a much worse trade.
    try:
        page.goto(base_url + "/", wait_until="commit")
        page.evaluate("() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }")
        # The next load of this tab starts watching reveals from the mount.
        page.evaluate("() => { try { sessionStorage.setItem('__spaWatch', '1'); } catch (e) {} }")
        page.context.clear_cookies()
    except Exception as exc:  # noqa: BLE001 — never fail a capture over storage
        warn(f"{route}: could not clear app state before capture ({exc}); a recorded basket may be baked in")
    # `commit`, not `networkidle`: the entrance fade runs while the page is
    # still loading, and waiting for the network to go quiet waits straight
    # past it. measure_entrance() does its own waiting.
    if not getattr(page, "_spa_watch_boot", False):
        page.add_init_script(REVEAL_WATCH_BOOT)
        page._spa_watch_boot = True
    page.goto(base_url + route, wait_until="commit")
    # Watch reveals from mount on: an element on screen at load plays its
    # reveal straight away, before any later snapshot could see it hidden.
    # The boot installs the watch before the app's first animation frame; a
    # page it could not (storage refused, too little content) is watched as
    # soon as it has content, which misses what already started.
    watched = None
    for _ in range(80):
        watched = page.evaluate("() => window.__spaWatched")
        if watched is not None or page.evaluate("() => document.readyState === 'complete'"):
            break
        page.wait_for_timeout(50)
    if watched is None:
        for _ in range(80):
            if page.evaluate("() => !!document.body && document.body.querySelectorAll('*').length > 20"):
                break
            page.wait_for_timeout(50)
        watched = page.evaluate(REVEAL_WATCH_JS)
    entrance = measure_entrance(page)
    page.wait_for_load_state("networkidle")
    if watched:
        page.evaluate(REVEAL_PROBE_JS)
        ambiguous = page.evaluate(REVEAL_AMBIGUOUS_JS, REVEAL_SHORT_VIEWPORT)
        # The route fade is picked as the largest element fading at load; a
        # reveal on screen at load can look just like it. One the app holds
        # for the viewport on a short screen is a reveal, not the entrance.
        check = ambiguous + ([entrance["path"]] if entrance else [])
        if check:
            timed, held = timed_reveals(page, base_url + route, check)
            if timed:
                page.evaluate("(ps) => { for (const p of ps) { const e = window.__spa.elAt(p); if (e && e.__spaReveal) e.__spaReveal.timed = true; } }", timed)
            if entrance and entrance["path"] in held:
                entrance = None
    settle(page, motion_timeout=SETTLE_CAP_MS)
    if inherited:
        # Recorded on another page of the same route pattern (the template):
        # replayed here only where this page has the same control at the same
        # place — the same label on the same path. Anything else is dropped
        # and said, never applied to whatever sits there instead.
        kept = page.evaluate(INHERIT_CHECK_JS, {"records": records, "links": links, "scroll": scroll,
                                                "forms": FORM_RECORDS.get(route, []),
                                                "chains": inherited.get("chains") or {}})
        dropped = refreshed_dropped + (len(records) - len(kept["records"])) + (len(links) - len(kept["links"])) \
            + (len(scroll) - len(kept["scroll"])) + (len(FORM_RECORDS.get(route, [])) - len(kept["forms"]))
        records = [records[i] for i in kept["records"]]
        links = [links[i] for i in kept["links"]]
        scroll = [scroll[i] for i in kept["scroll"]]
        FORM_RECORDS[route] = [FORM_RECORDS[route][i] for i in kept["forms"]]
        if kept["formCount"] != inherited.get("formCount"):
            FORM_SUCCESS[route] = []
        if dropped:
            warn(f"{route}: {dropped} control(s) recorded on {inherited['from']} are not on this page the same way "
                 "— left static here")
        report["pages"].setdefault(route_to_file(route), {}).update(
            {"inheritedFrom": inherited["from"], "inheritedDropped": dropped})
    reveals = page.evaluate(REVEAL_COLLECT_JS, entrance["path"] if entrance else None) if watched else []
    if reveals:
        report["pages"].setdefault(route_to_file(route), {})["reveals"] = len(reveals)
        if not has_runtime:
            # The runtime shows the reveals; a site with nothing else to replay
            # still needs it.
            (OUT / "assets").mkdir(parents=True, exist_ok=True)
            (OUT / "assets" / "spa-runtime.js").write_text(RUNTIME)
            has_runtime = True

    if links:
        swapped = page.evaluate(LINKS_JS, links)
        for n in swapped["notes"]:
            warn(f"{route}: {n}")
        report["pages"].setdefault(route_to_file(route), {})["linksFromButtons"] = swapped["swapped"]
    page.evaluate(FORM_RESOLVE_JS, FORM_RECORDS.get(route, []))
    notes = page.evaluate(APPLY_JS, {"records": records, "scroll": scroll, "groups": {}, "entrance": entrance})
    notes += page.evaluate(FORM_STAMP_JS)
    # What each form's valid submit showed (record_form_success), on the form.
    for fb in FORM_SUCCESS.get(route, []):
        rec = {k: fb[k] for k in ("kind", "html", "text", "title", "description", "list", "region", "ms") if k in fb}
        if not page.evaluate("([i, v]) => { const f = document.forms[i]; if (!f) return false; f.setAttribute('data-spa-success', v); return true; }",
                             [fb["form"], json.dumps(rec, ensure_ascii=False)]):
            notes.append(f"form {fb['form']} vanished before its success record could be written")
    for n in notes:
        warn(f"{route}: {n}")

    depth = "../" * (len(Path(route_to_file(route)).parts) - 1)
    stripped = page.evaluate(STRIP_AND_LINK_JS, {"routeMap": routemap, "depth": depth})
    for n in stripped["notes"]:
        warn(f"{route}: {n}")
    dropped_scripts.update(stripped["dropped"])

    if reveals:
        # Each reveal in document order, as the page is serialised: the one
        # element across routes (unify_reveal_durations) and its place in
        # the written file.
        REVEAL_PAGES[route] = {"file": out_file, "reveals": reveals, "order": page.evaluate(REVEAL_ORDER_JS)}
    html = page.evaluate("() => document.documentElement.outerHTML")
    # Losing the doctype puts every downstream render — gate A, gate B, the
    # editor preview — into quirks mode, where box sizing and line height
    # differ from the site being converted.
    html = "<!doctype html>\n" + html

    if has_runtime:
        tag = f'<script src="{depth}assets/spa-runtime.js" defer></script>'
        if "</head>" in html:
            html = html.replace("</head>", f"  {tag}\n</head>", 1)
        else:
            warn(f"{route}: no </head> to place the runtime in — behaviour will not replay")

    if entrance:
        # Pure CSS, in the head, so it runs at first paint. Doing this from
        # the deferred runtime instead would paint the page opaque and THEN
        # fade it, which reads as a flash rather than an entrance. It also
        # keeps the rule off the JS dependency chain: if anything stops the
        # script running, the page is simply visible, never stuck at 0.
        css = ('<style>@keyframes spa-enter{from{opacity:0}to{opacity:1}}'
               '[data-spa-enter]{animation:spa-enter %dms ease-in-out}'
               '@media (prefers-reduced-motion:reduce){[data-spa-enter]{animation:none}}'
               '</style>') % entrance["ms"]
        html = html.replace("</head>", f"  {css}\n</head>", 1)
        report["pages"].setdefault(route_to_file(route), {})["entranceMs"] = entrance["ms"]

    if reveals:
        html = html.replace("</head>", "  " + reveal_css(reveals) + "\n</head>", 1)

    residue = re.findall(r'style="[^"]*(?:scale\(|translate(?:X|Y|3d)?\()[^"]*"', html)
    if residue:
        warn(f"{route}: {len(residue)} inline transform(s) survived the settle — possible mid-animation capture: {residue[:2]}")

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(html)
    return html


# ---------------------------------------------------------------- gate

def _behavior_routes(job):
    """Gate -1b — the replay actually replays.

    The pixel gate compares two pages AT REST, and every recorded disclosure
    is closed at rest, so a runtime that does nothing at all scores 0.0% and
    passes. That is the same shape of blind spot this whole stage exists to
    close, one level up: the drawer and the FAQ would look perfect in every
    screenshot and open for nobody.

    So: load the STATIC file, click each trigger, and require the panel to
    become visible — then click again and require it to go away. Scroll
    records get the same treatment against their own threshold.

    Deliberately reads only the SHIPPED page — `[data-spa-toggle]` and
    `[data-spa-scroll]` as they exist in the delivered markup — never the
    in-memory recordings. A gate fed by the same data that produced the
    artifact proves the two agree; this one has to prove the artifact WORKS,
    so its only input is the artifact."""
    routes, static_url = job
    ok = True
    out_lines, pages = [], {}
    print = lambda *a, **k: out_lines.append(" ".join(str(x) for x in a))  # noqa: E731 — collected, printed by the parent
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for route in routes:
            key = route_to_file(route)
            ctx = browser.new_context(viewport={"width": 390, "height": 844})
            guard_context(ctx, static_url)
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)[:200]))
            page.goto(f"{static_url}/{key}", wait_until="networkidle")
            quiesce(page, 600)

            triggers = page.locator("[data-spa-toggle]")
            n = triggers.count()
            scroll_nodes = page.locator("[data-spa-scroll]")
            n_scroll = scroll_nodes.count()
            if n == 0 and n_scroll == 0:
                continue
            opened = 0
            for i in range(n):
                t = triggers.nth(i)
                tid = t.get_attribute("data-spa-toggle")
                panel = page.locator(f'[data-spa-panel="{tid}"]').first
                if panel.count() == 0:
                    # Attribute-only transition (a drawer that swaps its own
                    # classes, a gallery thumbnail): nothing appears, so what
                    # has to replay is the recorded values themselves, and the
                    # trigger's own label where one was recorded.
                    state_js = """([t, side]) => {
                      const bad = [];
                      for (const c of JSON.parse(t.getAttribute('data-spa-attrs') || '[]')) {
                        const el = document.querySelector('[data-spa-id="' + c.id + '"]');
                        if (el && el.getAttribute(c.attr) !== c[side]) bad.push(c.id + '@' + c.attr);
                      }
                      const inner = t.getAttribute('data-spa-inner');
                      if (inner && t.innerHTML !== JSON.parse(inner)[side]) bad.push('label');
                      return bad;
                    }"""
                    if not (t.get_attribute("data-spa-attrs") or t.get_attribute("data-spa-inner")):
                        continue
                    try:
                        if not t.is_visible():
                            continue
                        t.click(timeout=3000)
                        quiesce(page, 350)
                        bad = page.evaluate(state_js, [t.element_handle(), "on"])
                        if bad:
                            ok = False
                            print(f"  FAIL {key}: trigger {tid} did not apply its open state: {bad[:3]}")
                            continue
                        opened += 1
                        # A quantity stepper's button counts on a second press
                        # (1 -> 2 -> 3, the runtime's stepOf) — neither kept nor
                        # toggled back; its one recorded step is what replays.
                        if page.evaluate(COUNTER_JS, t.element_handle()):
                            continue
                        t.click(timeout=3000)
                        quiesce(page, 350)
                        # A swap-group member (a thumbnail, a size chip) is a
                        # radio button: clicking the chosen one again KEEPS it,
                        # as the application does. Any other trigger toggles.
                        side = "on" if t.get_attribute("data-spa-swap") else "off"
                        bad = page.evaluate(state_js, [t.element_handle(), side])
                        if bad:
                            ok = False
                            print(f"  FAIL {key}: trigger {tid} did not "
                                  + ("keep its chosen state" if side == "on" else "return to its closed state")
                                  + f": {bad[:3]}")
                    except Exception as e:
                        ok = False
                        print(f"  FAIL {key}: trigger {tid} unusable: {str(e)[:90]}")
                    continue
                try:
                    if not t.is_visible():
                        continue
                    if t.get_attribute("data-spa-starts-open") is not None:
                        # Open at rest: it must be showing, close on the
                        # first click and come back on the second.
                        if not panel.is_visible():
                            ok = False
                            print(f"  FAIL {key}: trigger {tid} starts open but its panel is hidden")
                            continue
                        t.click(timeout=3000)
                        quiesce(page, 350)
                        if panel.is_visible():
                            ok = False
                            print(f"  FAIL {key}: trigger {tid} did not close its open panel")
                            continue
                        opened += 1
                        t.click(timeout=3000)
                        quiesce(page, 350)
                        if not panel.is_visible():
                            ok = False
                            print(f"  FAIL {key}: trigger {tid} did not reopen its panel")
                        continue
                    t.click(timeout=3000)
                    quiesce(page, 350)
                    if not panel.is_visible():
                        ok = False
                        print(f"  FAIL {key}: trigger {tid} did not reveal its panel")
                        continue
                    opened += 1
                    t.click(timeout=3000)
                    quiesce(page, 350)
                    if panel.is_visible():
                        ok = False
                        print(f"  FAIL {key}: trigger {tid} did not close again")
                except Exception as e:
                    ok = False
                    print(f"  FAIL {key}: trigger {tid} unusable: {str(e)[:90]}")

            scrolled_ok = True
            if n_scroll:
                el = scroll_nodes.first
                threshold = json.loads(el.get_attribute("data-spa-scroll"))["y"]
                # Back to the top before reading the resting value, and with
                # smooth scrolling off. The trigger clicks above scroll the
                # page (a focused control pulls itself into view), so on a
                # page with nine disclosures the header has ALREADY swapped
                # by the time this check starts — `before` reads the scrolled
                # state, `after` reads the same, and the gate reports a
                # working site as broken. Verified live on the FAQ page.
                page.evaluate("""() => {
                  document.documentElement.style.scrollBehavior = 'auto';
                  window.scrollTo(0, 0);
                }""")
                quiesce(page, 450)
                before = el.get_attribute("class")
                page.evaluate("(y) => window.scrollTo(0, y + 80)", threshold)
                quiesce(page, 450)
                after = el.get_attribute("class")
                if before == after:
                    scrolled_ok = ok = False
                    print(f"  FAIL {key}: scroll state never changed past y={threshold}")

            if errors:
                ok = False
                print(f"  FAIL {key}: runtime threw: {errors[0]}")
            pages[key] = {
                "triggersOpened": opened, "triggersInMarkup": n,
                "scrollReplayed": scrolled_ok if n_scroll else None,
                "runtimeErrors": errors,
            }
            if ok:
                print(f"  ok   {key}: {opened}/{n} disclosure(s) open and close"
                      + (", scroll state replays" if n_scroll else ""))
            ctx.close()
        browser.close()
    return ok, out_lines, pages


def behavior_gate(routes, static_url):
    """Gate -1b, over the routes in --jobs processes (see _behavior_routes).
    Each route is one page with its own context either way, so splitting the
    list changes nothing it measures; the lines are printed in route order."""
    if args.jobs > 1 and len(routes) > 1:
        import concurrent.futures, multiprocessing
        n = min(args.jobs, len(routes))
        chunks = [routes[i::n] for i in range(n)]
        with concurrent.futures.ProcessPoolExecutor(max_workers=n, mp_context=multiprocessing.get_context("spawn")) as ex:
            parts = list(ex.map(_behavior_routes, [(c, static_url) for c in chunks]))
    else:
        parts = [_behavior_routes((routes, static_url))]
    ok = all(p[0] for p in parts)
    pages = {}
    lines = []
    for _, ls, pg in parts:
        lines.extend(ls)
        pages.update(pg)
    order = {route_to_file(r): i for i, r in enumerate(routes)}
    keyed = sorted(lines, key=lambda l: next((order[k] for k in order if f" {k}:" in l), 0))
    for line in keyed:
        print(line)
    for key, value in pages.items():
        report["pages"].setdefault(key, {})["behavior"] = value
    return ok


def _parity_pair(route, label, w, base_url, static_url, shots):
    """Capture the app and the static page at one width; return the diff ratio."""
    from PIL import Image, ImageChops
    key = route_to_file(route)
    imgs = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for side, url in (("app", base_url + route), ("static", static_url + "/" + key)):
            ctx = browser.new_context(viewport={"width": w, "height": 900}, device_scale_factor=1)
            guard_context(ctx, base_url if side == "app" else static_url)
            p = ctx.new_page()
            p.goto(url, wait_until="networkidle")
            settle(p)
            path = shots / f"{key.replace('/', '_')}.{label}.{side}.png"
            p.screenshot(path=str(path), full_page=True)
            imgs.append(path)
            ctx.close()
        browser.close()
    a, b = Image.open(imgs[0]).convert("RGB"), Image.open(imgs[1]).convert("RGB")
    if a.size != b.size:
        h = max(a.size[1], b.size[1])
        pad = lambda im: (lambda c: (c.paste(im, (0, 0)), c)[1])(Image.new("RGB", (max(a.size[0], b.size[0]), h), (255, 255, 255)))
        a, b = pad(a), pad(b)
    diff = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 24 else 0)
    ratio = sum(diff.histogram()[1:]) / float(a.size[0] * a.size[1])
    if ratio > args.threshold:
        diff.save(str(shots / f"{key.replace('/', '_')}.{label}.diff.png"))
    return ratio


def _parity_width(job):
    """One worker: every route at one width (its own browser per pair)."""
    routes, label, w, base_url, static_url, shots = job
    return [(route, label, _parity_pair(route, label, w, base_url, static_url, Path(shots))) for route in routes]


def parity_gate(routes, base_url, static_url):
    """Gate -1: the running app against the static capture, full page, at
    1440/820/390. With --jobs > 1 the three widths are measured at once, one
    process and browser each — the gate's 132 captures were the largest single
    share of a shop's prerender. The verdict stays a serial one: every pair
    that comes back over the threshold is captured again ALONE and only that
    measurement counts, the same discipline gate A's --jobs follows."""
    WIDTHS = [("desktop", 1440), ("tablet", 820), ("mobile", 390)]
    shots = REPORT.parent / "prerender-parity"
    shots.mkdir(parents=True, exist_ok=True)
    results = []
    if args.jobs > 1:
        import concurrent.futures, multiprocessing
        jobs = [(routes, label, w, base_url, static_url, str(shots)) for label, w in WIDTHS]
        with concurrent.futures.ProcessPoolExecutor(max_workers=min(args.jobs, len(jobs)),
                                                    mp_context=multiprocessing.get_context("spawn")) as ex:
            for part in ex.map(_parity_width, jobs):
                results.extend(part)
    else:
        for label, w in WIDTHS:
            results.extend(_parity_width((routes, label, w, base_url, static_url, str(shots))))
    ok = True
    by_label = dict(WIDTHS)
    for route, label, ratio in results:
        key = route_to_file(route)
        if ratio > args.threshold and args.jobs > 1:
            first = ratio
            ratio = _parity_pair(route, label, by_label[label], base_url, static_url, shots)
            print(f"  re-measured {key} @{label} alone: {first:.2%} -> {ratio:.2%}")
        page_report = report["pages"].setdefault(key, {})
        page_report.setdefault("parity", {})[label] = round(ratio, 5)
        if ratio > args.threshold:
            ok = False
            print(f"  FAIL {key} @{label}: {ratio:.2%} differs from the running app")
        else:
            print(f"  ok   {key} @{label}: {ratio:.2%}")
    return ok


# ---------------------------------------------------------------- main

def route_group(route, routes, linked, declared):
    """(group, declared): the group a route's pages share one recording in.
    DECLARED: the parameterised route of the app's own route table that the
    page belongs to — React Router's /product/:id, a TanStack route file's
    /blog/$slug. Otherwise its SHAPE — the same prefix, one varying last
    segment (/blog/<slug>), whatever the framework; a static export names its
    pages only by path, and a shared prefix is not always a shared template
    (/about/story, /about/care), so a shape group is a candidate
    (same_template decides)."""
    if route == CATCHALL_PROBE:
        return None, False
    if route in linked:
        return linked[route], True
    for pattern, rx in declared:
        if rx.match(route):
            return pattern, True
    segs = route.strip("/").split("/")
    if len(segs) < 2:
        return None, False
    prefix = "/" + "/".join(segs[:-1])
    siblings = [r for r in routes if r != route and r != CATCHALL_PROBE
                and r.strip("/").split("/")[:-1] == segs[:-1] and len(r.strip("/").split("/")) == len(segs)]
    return (f"{prefix}/*", False) if siblings else (None, False)


PROBE_JS = r"""
() => {
  // Where a control sits, as the chain of element names above it: a card's
  // button in the fifth card and in the ninth is one kind of control.
  const chain = (el) => {
    const out = [];
    for (let e = el; e && e !== document.documentElement; e = e.parentElement) out.unshift(e.tagName.toLowerCase());
    return out.join('>');
  };
  return {
    hrefs: [...document.querySelectorAll('a[href]')].map((a) => a.getAttribute('href')),
    controls: window.__spa.candidates().map((c) => [c.path, c.tag, c.label, chain(window.__spa.elAt(c.path))]),
    forms: document.forms.length,
  };
}
"""


def page_probe(page, url):
    """One load of a page at rest: its links, its controls and its forms."""
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(url, wait_until="networkidle")
    settle(page, quick=True)
    return page.evaluate(PROBE_JS)


def same_template(first, probe):
    """Is a page of a SHAPE group (no route table says so) its first page's
    template? Mostly the same kinds of controls in the same places — the
    chain of elements above each, so five sizes and three, four cards and
    nine, are one kind — and the same forms. A declared group needs no such
    test in Flash; its pages are one template by the app's own table."""
    shape = lambda p: {c[3] for c in p["controls"]}
    a, b = shape(first), shape(probe)
    if first["forms"] != probe["forms"]:
        return False
    if not a and not b:
        return True
    return len(a & b) / len(a | b) >= 0.8


def progress_note(text):
    """The stage's own progress, through progress.sh, so a UI shows "N of M"
    instead of a silent stage. Only inside a run (a workspace with a
    progress.json); never a failure of the capture."""
    ws = os.environ.get("H2WP_WORKSPACE", "")
    script = Path(__file__).resolve().parent / "progress.sh"
    if not ws or not (Path(ws) / "progress.json").is_file() or not script.is_file():
        return
    try:
        subprocess.run(["bash", str(script), "note", "-1", text], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass


def _capture_routes(job):
    """The capture of some routes, in a browser of its own (a worker process).
    Returns what capture() leaves in this process's globals, for the parent
    to merge: the report's page rows and warnings, the reveals, the dropped
    bundles and the times."""
    routes, base_url, routemap, has_runtime, records, forms, successes, inherit, done_before, total = job
    FORM_RECORDS.update(forms)
    FORM_SUCCESS.update(successes)
    KNOWN_ROUTES.update(routemap)
    times = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        guard_context(ctx, base_url)
        ctx.add_init_script(HELPERS)
        page = ctx.new_page()
        page.on("console", lambda m: warn(f"console {m.type}: {m.text[:160]}") if m.type == "error" else None)
        for n, route in enumerate(routes, 1):
            recs, scroll, links = records[route]
            print(f"- capturing {route} -> {route_to_file(route)}", flush=True)
            t0 = time.monotonic()
            capture(page, base_url, route, routemap, has_runtime, recs, scroll, links,
                    OUT / route_to_file(route), inherited=inherit.get(route))
            times[route] = round((time.monotonic() - t0) * 1000)
        browser.close()
    pages = {route_to_file(r): report["pages"].get(route_to_file(r), {}) for r in routes}
    reveals = {r: {**v, "file": str(v["file"])} for r, v in REVEAL_PAGES.items()}
    return {"pages": pages, "warnings": report["warnings"], "reveals": reveals,
            "dropped": sorted(dropped_scripts), "times": times,
            "runtime": (OUT / "assets" / "spa-runtime.js").is_file()}


def capture_all(routes, all_records, base_url, routemap, has_runtime, timing):
    """Every route's capture, in --jobs browsers at once. Each route is one
    page in its own browser context either way, so splitting the list changes
    nothing a capture records; only the wall clock."""
    inherit = {}
    for route in routes:
        first = (timing["routes"].get(route) or {}).get("inheritedFrom")
        if first:
            inherit[route] = {"from": first, "formCount": FORM_COUNT.get(first),
                              "chains": TEMPLATE_CHAINS.get(first, {})}
    records = {r: all_records[r] for r in routes}
    n = max(1, min(args.jobs, len(routes)))
    chunks = [routes[i::n] for i in range(n)]
    jobs = [(c, base_url, routemap, has_runtime, records, {r: FORM_RECORDS.get(r, []) for r in c},
             {r: FORM_SUCCESS.get(r, []) for r in c}, {r: inherit[r] for r in c if r in inherit}, 0, len(routes))
            for c in chunks]
    print(f"- capturing {len(routes)} route(s) in {n} browser(s)", flush=True)
    progress_note(f"capturing the pages: {len(routes)} in {n} browser(s)")
    if n > 1:
        import concurrent.futures, multiprocessing
        with concurrent.futures.ProcessPoolExecutor(max_workers=n, mp_context=multiprocessing.get_context("spawn")) as ex:
            parts = []
            for i, part in enumerate(ex.map(_capture_routes, jobs), 1):
                parts.append(part)
    else:
        before = len(report["warnings"])
        part = _capture_routes(jobs[0])
        part["warnings"] = part["warnings"][before:]
        parts = [part]
    for part in parts:
        for key, row in part["pages"].items():
            report["pages"].setdefault(key, {}).update(row)
        if n > 1:
            report["warnings"].extend(part["warnings"])
        for route, rec in part["reveals"].items():
            REVEAL_PAGES[route] = {**rec, "file": Path(rec["file"])}
        dropped_scripts.update(part["dropped"])
        for route, ms in part["times"].items():
            timing["routes"].setdefault(route, {})["captureMs"] = ms
    timing["captureJobs"] = n


def main():
    global DIST
    built = False
    if TANSTACK and not args.routes and not args.gates_only:
        # The routes are known only once the framework has written its pages.
        build()
        built = True
        routes = routes_from_output(DIST)
        report["routesFrom"] = "tanstack prerender output"
    else:
        routes = None
    if routes is not None:
        dynamic = []
        has_catchall = False
    elif args.routes:
        routes, has_catchall = named_routes(args.routes)
        dynamic = []
    else:
        routes, dynamic = discover_routes()
        has_catchall = any("*" in d for d in dynamic)
        routes = [r for r in routes if r not in ("/*",)]
    if not routes and not dynamic and not args.gates_only:
        # No route table to read (no React Router): a build that wrote one
        # HTML file per page — Astro, a Vite multi-page app, a static export —
        # names its pages itself. Only its own pages are taken; a build that
        # wrote nothing but the app shell still has no routes.
        build()
        built = True
        written = routes_from_output(DIST)
        if written and written != ["/"]:
            routes = written
            report["routesFrom"] = "build output"
    if not routes:
        print("no routes discovered — pass --routes=/,/about,…", file=sys.stderr)
        sys.exit(2)
    if has_catchall:
        routes.append(CATCHALL_PROBE)
    report["skippedRoutes"] = [d for d in dynamic if "*" not in d]
    # A parameterised route's pages are the ones the app links to: they are
    # captured below as the recording finds them. Warned only when none is.
    patterns = [] if args.gates_only else param_route_patterns(report["skippedRoutes"])
    linked = {}
    if args.gates_only:
        # The pages a full run found through links are gated again too.
        try:
            earlier = json.loads(REPORT.read_text()).get("linkedRoutes", [])
        except (OSError, ValueError):
            earlier = []
        for row in earlier:
            if isinstance(row, dict) and isinstance(row.get("route"), str) and row["route"] not in routes \
                    and any(rx.match(row["route"]) for _, rx in param_route_patterns(report["skippedRoutes"])):
                routes.append(row["route"])
                linked[row["route"]] = row.get("of", "")
        for d in report["skippedRoutes"]:
            if d not in linked.values():
                warn(f"route {d} is parameterised — no data to prerender it from; not converted")
    report["routes"] = routes
    print(f"- {len(routes)} route(s): {', '.join(routes)}")

    if args.gates_only:
        if not (OUT / "index.html").exists():
            print(f"--gates-only needs an existing capture in {OUT}", file=sys.stderr)
            sys.exit(2)
        # Reuse exactly the prepared baseline, without downloads or mutations.
        previous = json.loads(REPORT.read_text()) if REPORT.is_file() else {}
        prepared = OUT.parent / ('.' + OUT.name + '-source-build')
        if previous.get('dist') == str(prepared):
            if not prepared.is_dir() or prepared.is_symlink():
                raise ValueError('prepared source baseline is missing or unsafe; cannot certify a different input')
            DIST = prepared
        report['dist'] = str(DIST)
        if 'sourceAssets' in previous:
            report['sourceAssets'] = previous['sourceAssets']
        dist_srv, base_url = serve(DIST, spa_fallback=True)
        static_srv, static_url = serve(OUT, spa_fallback=False)
        try:
            print("- gate -1b: recorded behaviour replays on the static page", flush=True)
            t0 = time.monotonic()
            behaved = behavior_gate(routes, static_url)
            t1 = time.monotonic()
            print("- gate -1: running app vs static capture", flush=True)
            pixels = parity_gate(routes, base_url, static_url)
            report.setdefault("timing", {"routes": {}}).update({
                "gateBehaviourMs": round((t1 - t0) * 1000),
                "gateParityMs": round((time.monotonic() - t1) * 1000)})
            report["passed"] = behaved and pixels
        finally:
            static_srv.shutdown()
            dist_srv.shutdown()
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, indent=2))
        print(f"\nreport: {REPORT}")
        # Same closing line as a full run, so anything watching the log for a
        # verdict (a CI step, a chained command) sees one string either way.
        print("gate passed — this directory is now a valid stage 0 input"
              if report["passed"] else "GATE FAILED — inspect the report; Full uses bounded repairs then delivers with recorded issues")
        sys.exit(0 if report["passed"] else 1)

    if not built:
        build()

    DIST, asset_report = source_assets.prepare_build(
        DIST, PROJECT, OUT, args.asset_origin, OUT.parent / 'source-assets-report.json')
    report['dist'] = str(DIST)
    report['sourceAssets'] = asset_report
    if asset_report['unresolved']:
        warn(f"{len(asset_report['unresolved'])} source image(s) missing; see source-assets-report.json")

    if OUT.exists():
        if not (OUT / MARKER).exists() and any(OUT.iterdir()) and not args.force:
            print(f"{OUT} is not empty and was not written by this script — pass --force", file=sys.stderr)
            sys.exit(2)
        shutil.rmtree(OUT)
    try:
        copied = sandbox.copy_build_output(DIST, OUT)
    except (OSError, ValueError) as err:
        print(f"refusing unsafe build output: {err}", file=sys.stderr)
        sys.exit(2)
    print(f"- copied {copied['files']} regular output file(s), {copied['bytes']} bytes")
    (OUT / MARKER).write_text("written by prerender-spa.py\n")
    for stale in OUT.rglob("*.html"):
        stale.unlink()

    routemap = {r: route_to_file(r) for r in routes if r != CATCHALL_PROBE}
    routemap["/"] = "index.html"
    KNOWN_ROUTES.update(routemap)

    dist_srv, base_url = serve(DIST, spa_fallback=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
            guard_context(ctx, base_url)
            # Installed once, re-run by the browser on every navigation —
            # rather than re-evaluated by hand after each goto, which is one
            # forgotten call away from `window.__spa is undefined` in the
            # middle of a recording run.
            ctx.add_init_script(HELPERS)
            page = ctx.new_page()
            page.on("console", lambda m: warn(f"console {m.type}: {m.text[:160]}") if m.type == "error" else None)

            all_records = {}
            timing = report.setdefault("timing", {"routes": {}})
            # One recording per route PATTERN. The pages of /product/:id are
            # one template filled with different data: its controls are
            # recorded on the first page of the pattern and replayed on the
            # others where the same control sits at the same place (checked
            # at capture). Recording them all drove every control of every
            # product page, per width, with a reload per navigating control —
            # measured on a 45-page shop: over an hour for the same template.
            template, template_probe = {}, {}
            recorded = 0

            def follow(hrefs):
                """Pages of the table's parameterised routes this page links to,
                appended to the list being walked: recorded (or grouped),
                captured and gated like any other route; their own links are
                followed in turn."""
                if not patterns or len(linked) >= MAX_LINKED_ROUTES:
                    return
                for path, fam in linked_route_instances(hrefs, patterns, set(routes)):
                    if len(linked) >= MAX_LINKED_ROUTES:
                        break
                    routes.append(path)
                    routemap[path] = route_to_file(path)
                    KNOWN_ROUTES.add(path)
                    linked[path] = fam
                    print(f"  + {path} (linked page of {fam})", flush=True)

            # The app's own parameterised routes: React Router's table, and
            # TanStack Router's route files (/blog/$slug).
            declared = param_route_patterns(report["skippedRoutes"] + tanstack_param_routes(PROJECT))
            for route in routes:
                url = base_url + route
                recorded += 1
                # Flash only: Full records every page on its own.
                family, is_declared = route_group(route, routes, linked, declared) if args.flash else (None, False)
                if family and family in template:
                    first = template[family]
                    t0 = time.monotonic()
                    probe = page_probe(page, url)
                    # A page of a declared group IS its first page's template
                    # (the app's own table says so): never recorded on its
                    # own in Flash; what it has that the first page did not
                    # stays static, counted at capture.
                    same = is_declared or same_template(template_probe[first], probe)
                    follow(probe["hrefs"])
                    if same:
                        print(f"- recording {route} ({recorded} of {len(routes)}) — the {family} template, "
                              f"recorded on {first}", flush=True)
                        progress_note(f"recording the pages: {recorded} of {len(routes)}")
                        all_records[route] = all_records[first]
                        FORM_RECORDS[route] = FORM_RECORDS[first]
                        FORM_SUCCESS[route] = FORM_SUCCESS[first]
                        timing["routes"][route] = {"inheritedFrom": first,
                                                   "discoverMs": round((time.monotonic() - t0) * 1000)}
                        first_page = report["pages"].get(route_to_file(first), {})
                        report["pages"].setdefault(route_to_file(route), {}).update({
                            **{k: first_page[k] for k in ("scrollStateElements", "scrollThreshold", "singleSelectGroups",
                                                          "formSuccess", "links", "formValidation") if k in first_page},
                            "route": route, "inheritedFrom": first})
                        continue
                    print(f"- recording {route} ({recorded} of {len(routes)}) — in {family}, but its controls are "
                          f"not {first}'s: recorded on its own", flush=True)
                else:
                    print(f"- recording {route} ({recorded} of {len(routes)})", flush=True)
                progress_note(f"recording the pages: {recorded} of {len(routes)}")
                t0 = time.monotonic()
                recs, links = record_interactions(page, url)
                t1 = time.monotonic()
                groups = detect_single_select(page, url, recs) if len(recs) > 1 else {}
                scope_group_changes(recs)
                detect_close_on_link(page, url, recs, links)
                t2 = time.monotonic()
                scroll = record_scroll_state(page, url)
                t3 = time.monotonic()
                FORM_RECORDS[route] = record_form_validation(page, url)
                FORM_SUCCESS[route] = record_form_success(page, url)
                t4 = time.monotonic()
                timing["routes"][route] = {"interactionsMs": round((t1 - t0) * 1000),
                                           "groupsMs": round((t2 - t1) * 1000),
                                           "scrollMs": round((t3 - t2) * 1000),
                                           "formsMs": round((t4 - t3) * 1000),
                                           "records": len(recs)}
                all_records[route] = (recs, scroll, links)
                # The page as it rests: its links (the pages to follow), and —
                # for the first page of a group — the controls its siblings
                # must have to share this recording, and its form count.
                probe = page_probe(page, url)
                if family and family not in template:
                    template[family] = route
                    template_probe[route] = probe
                    FORM_COUNT[route] = probe["forms"]
                    TEMPLATE_CHAINS[route] = {c[0]: c[3] for c in probe["controls"]}
                follow(probe["hrefs"])
                report["pages"].setdefault(route_to_file(route), {}).update({
                    "route": route,
                    "scrollStateElements": len(scroll),
                    "scrollThreshold": (scroll[0]["y"] if scroll else None),
                    "singleSelectGroups": groups,
                    "formSuccess": [{k: f[k] for k in ("form", "kind", "text", "ms")} for f in FORM_SUCCESS[route]],
                    "links": [{"label": l["label"], "to": l["to"]} for l in links],
                    # What an empty submit printed, per form; replayed by the runtime.
                    "formValidation": [[{"text": a["text"], "field": bool(a["field"]), **({"ttlMs": a["ttl"]} if a.get("ttl") else {})}
                                        for a in f["added"]] for f in FORM_RECORDS[route]],
                })

            report["linkedRoutes"] = [{"route": p, "of": f} for p, f in linked.items()]
            for d in report["skippedRoutes"]:
                if d not in linked.values():
                    warn(f"route {d} is parameterised and no captured page links to one — not converted")
            if len(linked) >= MAX_LINKED_ROUTES:
                warn(f"stopped following links after {MAX_LINKED_ROUTES} pages of parameterised routes")
            report["routes"] = routes

            # Close-on-link is a property of the CONTROL, not of the page it was
            # recorded on. The drawer is shared chrome, and only the page its
            # sections live on has a same-page link to probe it with; stamping
            # just there would make that page's header differ from every other
            # page's, splitting one header into two design groups. So a verdict
            # seen anywhere is carried to the same control (same path, same
            # label) wherever it went unprobed — never over a page that
            # measured the opposite.
            verdicts = {}
            for recs, _, _ in all_records.values():
                for r in recs:
                    if "closeOnLink" in r:
                        verdicts.setdefault((r["trigger"], r["label"]), set()).add(r["closeOnLink"])
            for key, seen in verdicts.items():
                if len(seen) > 1:
                    warn(f"{key[1] or key[0]}: closes on an in-panel link on some pages and not others "
                         f"— stamped per page, so this control will not be page-invariant")
            for route, (recs, _, _) in all_records.items():
                for r in recs:
                    seen = verdicts.get((r["trigger"], r["label"]), set())
                    if "closeOnLink" not in r and len(seen) == 1:
                        r["closeOnLink"] = next(iter(seen))
                report["pages"][route_to_file(route)]["disclosures"] = [
                    {"label": r["label"], "panels": len(r["panels"]),
                     "text": (r["panels"][0]["text"] if r["panels"] else ""),
                     "group": r.get("group"), "recordedAt": r["width"],
                     "closeOnLink": bool(r.get("closeOnLink"))}
                    for r in recs
                ]

            has_runtime = any(recs or scroll for recs, scroll, _ in all_records.values()) or any(FORM_RECORDS.values())
            if has_runtime:
                (OUT / "assets").mkdir(parents=True, exist_ok=True)
                (OUT / "assets" / "spa-runtime.js").write_text(RUNTIME)

            browser.close()
        capture_all(routes, all_records, base_url, routemap, has_runtime, timing)

        unified = unify_reveal_durations(REVEAL_PAGES)
        if unified:
            report["revealDurations"] = unified
            for u in unified:
                print(f"- one reveal duration, {u['ms']} ms, for {u['element']} on every route (measured {u['measured']})", flush=True)

        # The framework bundle was copied in with the rest of dist/ and is now
        # referenced by nothing. Leaving it means the delivered THEME ships
        # half a megabyte of React that no page loads — and it is not inert:
        # stage 0 reads every file looking for copy that lives only in
        # JavaScript, finds React's own minified error strings, and reports
        # the site as having seven unreachable blocks of prose. Deleting only
        # the paths actually removed from a <script>/modulepreload keeps this
        # precise: no heuristic sweep of *.js, nothing else touched.
        pruned = []
        for src in sorted(dropped_scripts):
            f = OUT / src.split("?")[0].lstrip("/")
            if f.is_file() and OUT in f.parents:
                pruned.append(f"{f.relative_to(OUT)} ({f.stat().st_size // 1024}KB)")
                f.unlink()
        report["prunedBundles"] = pruned
        if pruned:
            print(f"- pruned {len(pruned)} unreferenced bundle(s): {', '.join(pruned)}")

        if args.no_verify:
            warn("parity gate skipped (--no-verify) — React->prerender drift is unmeasured")
        else:
            static_srv, static_url = serve(OUT, spa_fallback=False)
            try:
                print("- gate -1b: recorded behaviour replays on the static page", flush=True)
                t0 = time.monotonic()
                behaved = behavior_gate(routes, static_url)
                t1 = time.monotonic()
                print("- gate -1: running app vs static capture", flush=True)
                pixels = parity_gate(routes, base_url, static_url)
                report.setdefault("timing", {"routes": {}}).update({
                    "gateBehaviourMs": round((t1 - t0) * 1000),
                    "gateParityMs": round((time.monotonic() - t1) * 1000)})
                report["passed"] = behaved and pixels
            finally:
                static_srv.shutdown()
    finally:
        dist_srv.shutdown()

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2))
    print(f"\nreport: {REPORT}")
    print(f"static site: {OUT}")
    if not report["passed"]:
        print("\nGATE FAILED — the static capture differs from the running app. Full: attempt bounded repairs, then use a usable capture and report the failed checks.",
              file=sys.stderr)
        sys.exit(1)
    print("gate passed — this directory is now a valid stage 0 input")


if __name__ == "__main__":
    main()
