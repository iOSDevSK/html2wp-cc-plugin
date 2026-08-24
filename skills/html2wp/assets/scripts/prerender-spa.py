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

import argparse, functools, json, os, re, shutil, subprocess, sys, threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import sandbox  # noqa: E402
from net_guard import attach_network_guard  # noqa: E402

from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--project", required=True, help="the SPA project root (has package.json)")
ap.add_argument("--out", required=True, help="directory to write flat HTML into — the input to stage 0")
ap.add_argument("--routes", default="", help="comma-separated route paths; omit to discover from the source")
ap.add_argument("--dist", default="", help="build output dir (default <project>/dist)")
ap.add_argument("--build-cmd", default="npm run build")
ap.add_argument("--skip-build", action="store_true")
ap.add_argument("--no-verify", action="store_true", help="skip the React->prerender parity gate (never on a real conversion)")
ap.add_argument("--threshold", type=float, default=0.006, help="same 0.6%% as gate A")
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
DIST = Path(args.dist).resolve() if args.dist else PROJECT / "dist"
REPORT = Path(args.report).resolve() if args.report else OUT.parent / "prerender-report.json"
MARKER = ".prerender-spa"

report = {
    "project": str(PROJECT), "out": str(OUT), "routes": [], "pages": {},
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
            # slug?) and this pipeline has none. Report it rather than
            # inventing an instance of it.
            if ":" in p or "*" in p:
                dynamic.append(p)
                continue
            if p not in found:
                found.append(p)
    return found, dynamic


CATCHALL_PROBE = "/prerender-spa-404-probe"


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


# ---------------------------------------------------------------- build

def build():
    global DIST

    def usable_prebuilt():
        """Return an existing regular output without following a symlink root."""
        for candidate in (DIST, PROJECT / "dist", PROJECT / "build", PROJECT / "out"):
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
        candidates.extend((work / "dist", work / "build", work / "out"))
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
    class Handler(SimpleHTTPRequestHandler):
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
window.__spa = {
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
    return {
      attrChanges,
      panels,
      triggerInnerOn: triggerInner && trigger ? trigger.innerHTML : null,
    };
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
      const says = (el.getAttribute('aria-label') || el.textContent || '').trim();
      if (/\badd to (cart|bag|basket|tote)\b|\bbuy( now| it)?\b|\bcheckout\b|\bplace order\b|\bsubscribe\b|\bremove\b|\bdelete\b|\bclear\b/i.test(says)) continue;
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

    def wait_motion():
        last, stable, waited = None, 0, 0
        while waited < motion_timeout:
            sig = page.evaluate(sig_js)
            stable = stable + 1 if sig == last else 0
            last = sig
            if stable >= 3:
                return True
            page.wait_for_timeout(300)
            waited += 300
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
        page.wait_for_timeout(900)
        return

    page.evaluate("""async () => {
      const h = document.body.scrollHeight;
      for (let y = 0; y < h; y += 600) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 90)); }
      window.scrollTo(0, 0);
    }""")
    if not wait_motion():
        warn("entrance motion never settled within 26s — capture may hold a mid-animation transform")

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

def settle_scroll(page):
    """Return the page to scroll 0 and let scroll-reactive state catch up.

    Anything a component does BECAUSE the page moved is not part of the
    transition being recorded, and a scroll listener is a state update like
    any other — it needs a frame or two after the scroll to land."""
    page.evaluate("() => { document.documentElement.style.scrollBehavior = 'auto'; window.scrollTo(0, 0); }")
    page.wait_for_timeout(450)


def record_interactions(page, url, widths=(390, 1440)):
    """Drive every disclosure control the page has, at each width, and write
    down what it did. Both widths matter and neither is optional: a mobile
    drawer's trigger is `lg:hidden`, so at 1440 it cannot be clicked at all,
    and a desktop-only disclosure is equally invisible at 390."""
    records, seen = [], set()
    for w in widths:
        page.set_viewport_size({"width": w, "height": 900})
        page.goto(url, wait_until="networkidle")
        settle(page, quick=True)
        cands = page.evaluate("() => window.__spa.candidates()")
        for c in cands:
            key = c["path"]
            if key in seen:
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
                before_inner = el.evaluate("e => e.innerHTML")
                before_url = page.url
                el.click(timeout=2500)
            except Exception:
                continue
            page.wait_for_timeout(700)
            settle_scroll(page)
            if page.url != before_url:
                # A control that navigates is a link wearing a button's
                # clothes; it discloses nothing and the router has already
                # left the page we were recording.
                report["warnings"].append(f"{c['label'] or c['path']}: navigates, not a disclosure — skipped")
                page.goto(url, wait_until="networkidle")
                settle(page, quick=True)
                continue
            d = page.evaluate("(p) => window.__spa.diff(p)", c["path"])
            if not d["panels"] and not d["attrChanges"] and not d["triggerInnerOn"]:
                continue  # inert candidate — the wide net doing its job
            seen.add(key)
            records.append({
                "trigger": c["path"], "label": c["label"], "width": w,
                "panels": d["panels"],
                "attrChanges": [a for a in d["attrChanges"] if a["attr"] != "style"],
                "triggerInner": ({"off": before_inner, "on": d["triggerInnerOn"]}
                                 if d["triggerInnerOn"] is not None else None),
            })
            # Restore. Radix and every hand-rolled toggle close on a second
            # click; anything that does not gets a reload, because recording
            # the NEXT control against a dirty baseline produces a diff that
            # describes two transitions at once.
            try:
                el.click(timeout=2500)
                page.wait_for_timeout(500)
            except Exception:
                pass
            clean = page.evaluate("() => document.querySelectorAll('*').length === window.__spaBase.filter(r => r[0].isConnected).length")
            if not clean:
                page.goto(url, wait_until="networkidle")
                settle(page, quick=True)
    return records


def detect_single_select(page, url, records):
    """A group of disclosures is single-select when opening one closes the
    last. It cannot be inferred from one control at a time — each was
    recorded against a clean baseline on purpose — so it needs its own
    probe: open A, then open B, and see whether A's panel survived.

    Skipping this is how a converted accordion ends up with every panel
    open at once the moment a visitor clicks twice."""
    groups = {}
    # Structural siblings, not DOM siblings. A Radix accordion nests its
    # trigger two levels inside the item (item > h3 > button), so the
    # triggers are never each other's siblings and a parent-path key puts
    # each one in a group of its own — which reads as "no group" and ships
    # an accordion that opens every panel at once. Two triggers belong to one
    # group when their paths have the same LENGTH and differ in exactly one
    # segment: that is precisely "the same control, one repeat over".
    buckets = []
    for r in records:
        if not r["panels"]:
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
    gid = 0
    for rs in buckets:
        if len(rs) < 2:
            continue
        a, b = rs[0], rs[1]
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
                page.wait_for_timeout(600)
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
    page.wait_for_timeout(700)  # let the class transition finish before reading
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

  function applyAttrs(changes, on) {
    for (var i = 0; i < changes.length; i++) {
      var c = changes[i];
      var target = document.querySelector('[data-spa-id="' + c.id + '"]');
      if (!target) continue;
      var v = on ? c.on : c.off;
      if (v === null || v === undefined) target.removeAttribute(c.attr);
      else target.setAttribute(c.attr, v);
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
    var id = trigger.getAttribute('data-spa-toggle');
    var panels = document.querySelectorAll('[data-spa-panel="' + id + '"]');
    var boxes = animatedBoxes(panels);
    var attrs = parse(trigger, 'data-spa-attrs', []);
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
        var s = p.getAttribute('data-spa-style') || '';
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
      trigger.innerHTML = on ? inner.on : inner.off;
      // The rewrite may have replaced a scroll-recorded element with a fresh
      // node in its RESTING classes — and a fresh node is invisible to a
      // disconnect check, because nothing that IS bound went anywhere. So the
      // applier is told outright to rebuild its list and re-apply.
      window.dispatchEvent(new Event('spa:scroll-rebind'));
    }
    trigger.setAttribute('data-spa-open', on ? 'true' : 'false');
    // Kept in sync even when the original never managed it. A hand-rolled
    // drawer routinely ships without aria-expanded; announcing the state is
    // an accessibility gain that costs no pixels, and the editor's smoke
    // test asserts this attribute flips.
    trigger.setAttribute('aria-expanded', on ? 'true' : 'false');
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
        var id = trigger.getAttribute('data-spa-toggle');
        if (document.querySelector('[data-spa-panel="' + id + '"]')) {
          setOpen(trigger, false);
        } else {
          trigger.setAttribute('data-spa-open', 'false');
        }
        trigger.addEventListener('click', function (ev) {
          ev.preventDefault();
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
          if (sp.add || sp.remove) {
            // A token delta, so the element keeps every class the delta does
            // not mention — its active-nav state above all. Replacing the
            // whole attribute would overwrite that with the state of
            // whichever page this shared chrome was recorded on.
            var gone = past ? (sp.remove || []) : (sp.add || []);
            var here = past ? (sp.add || []) : (sp.remove || []);
            if (gone.length) el.classList.remove.apply(el.classList, gone);
            if (here.length) el.classList.add.apply(el.classList, here);
          } else {
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

  if (document.readyState !== 'loading') init();
  else document.addEventListener('DOMContentLoaded', init);
})();
"""


# ------------------------------------------------------- apply + serialize

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

  records.forEach((rec, ri) => {
    const trigger = window.__spa.elAt(rec.trigger);
    if (!trigger) { notes.push('trigger vanished: ' + rec.trigger); return; }
    const tid = 't' + (ri + 1);
    trigger.setAttribute('data-spa-toggle', tid);
    if (rec.group) trigger.setAttribute('data-spa-group', rec.group);

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
      const after = p.afterPath ? window.__spa.elAt(p.afterPath) : null;
      if (after && after.parentElement === parent) after.insertAdjacentElement('afterend', node);
      else parent.insertBefore(node, parent.firstChild);
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


def capture(page, base_url, route, routemap, has_runtime, records, scroll, out_file):
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
        page.context.clear_cookies()
    except Exception as exc:  # noqa: BLE001 — never fail a capture over storage
        warn(f"{route}: could not clear app state before capture ({exc}); a recorded basket may be baked in")
    # `commit`, not `networkidle`: the entrance fade runs while the page is
    # still loading, and waiting for the network to go quiet waits straight
    # past it. measure_entrance() does its own waiting.
    page.goto(base_url + route, wait_until="commit")
    entrance = measure_entrance(page)
    page.wait_for_load_state("networkidle")
    settle(page)

    notes = page.evaluate(APPLY_JS, {"records": records, "scroll": scroll, "groups": {}, "entrance": entrance})
    for n in notes:
        warn(f"{route}: {n}")

    depth = "../" * (len(Path(route_to_file(route)).parts) - 1)
    stripped = page.evaluate(STRIP_AND_LINK_JS, {"routeMap": routemap, "depth": depth})
    for n in stripped["notes"]:
        warn(f"{route}: {n}")
    dropped_scripts.update(stripped["dropped"])

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

    residue = re.findall(r'style="[^"]*(?:scale\(|translate(?:X|Y|3d)?\()[^"]*"', html)
    if residue:
        warn(f"{route}: {len(residue)} inline transform(s) survived the settle — possible mid-animation capture: {residue[:2]}")

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(html)
    return html


# ---------------------------------------------------------------- gate

def behavior_gate(routes, static_url):
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
    ok = True
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
            page.wait_for_timeout(600)

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
                    continue  # attribute-only transition; nothing to reveal
                try:
                    if not t.is_visible():
                        continue
                    t.click(timeout=3000)
                    page.wait_for_timeout(350)
                    if not panel.is_visible():
                        ok = False
                        print(f"  FAIL {key}: trigger {tid} did not reveal its panel")
                        continue
                    opened += 1
                    t.click(timeout=3000)
                    page.wait_for_timeout(350)
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
                page.wait_for_timeout(450)
                before = el.get_attribute("class")
                page.evaluate("(y) => window.scrollTo(0, y + 80)", threshold)
                page.wait_for_timeout(450)
                after = el.get_attribute("class")
                if before == after:
                    scrolled_ok = ok = False
                    print(f"  FAIL {key}: scroll state never changed past y={threshold}")

            if errors:
                ok = False
                print(f"  FAIL {key}: runtime threw: {errors[0]}")
            report["pages"].setdefault(key, {})["behavior"] = {
                "triggersOpened": opened, "triggersInMarkup": n,
                "scrollReplayed": scrolled_ok if n_scroll else None,
                "runtimeErrors": errors,
            }
            if ok:
                print(f"  ok   {key}: {opened}/{n} disclosure(s) open and close"
                      + (", scroll state replays" if n_scroll else ""))
            ctx.close()
        browser.close()
    return ok


def parity_gate(routes, base_url, static_url):
    from PIL import Image, ImageChops
    WIDTHS = [("desktop", 1440), ("tablet", 820), ("mobile", 390)]
    shots = REPORT.parent / "prerender-parity"
    shots.mkdir(parents=True, exist_ok=True)
    ok = True
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for route in routes:
            key = route_to_file(route)
            page_report = report["pages"].setdefault(key, {})
            page_report["parity"] = {}
            for label, w in WIDTHS:
                imgs = []
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
                a, b = Image.open(imgs[0]).convert("RGB"), Image.open(imgs[1]).convert("RGB")
                if a.size != b.size:
                    h = max(a.size[1], b.size[1])
                    pad = lambda im: (lambda c: (c.paste(im, (0, 0)), c)[1])(Image.new("RGB", (max(a.size[0], b.size[0]), h), (255, 255, 255)))
                    a, b = pad(a), pad(b)
                diff = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 24 else 0)
                ratio = sum(diff.histogram()[1:]) / float(a.size[0] * a.size[1])
                page_report["parity"][label] = round(ratio, 5)
                if ratio > args.threshold:
                    ok = False
                    diff.save(str(shots / f"{key.replace('/', '_')}.{label}.diff.png"))
                    print(f"  FAIL {key} @{label}: {ratio:.2%} differs from the running app")
                else:
                    print(f"  ok   {key} @{label}: {ratio:.2%}")
        browser.close()
    return ok


# ---------------------------------------------------------------- main

def main():
    if args.routes:
        routes = [r.strip() for r in args.routes.split(",") if r.strip()]
        dynamic = []
        has_catchall = False
    else:
        routes, dynamic = discover_routes()
        has_catchall = any("*" in d for d in dynamic)
        routes = [r for r in routes if r not in ("/*",)]
    if not routes:
        print("no routes discovered — pass --routes=/,/about,…", file=sys.stderr)
        sys.exit(2)
    if has_catchall:
        routes.append(CATCHALL_PROBE)
    report["skippedRoutes"] = [d for d in dynamic if "*" not in d]
    for d in report["skippedRoutes"]:
        warn(f"route {d} is parameterised — no data to prerender it from; not converted")
    report["routes"] = routes
    print(f"- {len(routes)} route(s): {', '.join(routes)}")

    if args.gates_only:
        if not (OUT / "index.html").exists():
            print(f"--gates-only needs an existing capture in {OUT}", file=sys.stderr)
            sys.exit(2)
        dist_srv, base_url = serve(DIST, spa_fallback=True)
        static_srv, static_url = serve(OUT, spa_fallback=False)
        try:
            print("- gate -1b: recorded behaviour replays on the static page")
            behaved = behavior_gate(routes, static_url)
            print("- gate -1: running app vs static capture")
            pixels = parity_gate(routes, base_url, static_url)
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
              if report["passed"] else "GATE FAILED — do not proceed to stage 0")
        sys.exit(0 if report["passed"] else 1)

    build()

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
            for route in routes:
                url = base_url + route
                print(f"- recording {route}")
                recs = record_interactions(page, url)
                groups = detect_single_select(page, url, recs) if len(recs) > 1 else {}
                scroll = record_scroll_state(page, url)
                all_records[route] = (recs, scroll)
                report["pages"].setdefault(route_to_file(route), {}).update({
                    "route": route,
                    "disclosures": [
                        {"label": r["label"], "panels": len(r["panels"]),
                         "text": (r["panels"][0]["text"] if r["panels"] else ""),
                         "group": r.get("group"), "recordedAt": r["width"]}
                        for r in recs
                    ],
                    "scrollStateElements": len(scroll),
                    "scrollThreshold": (scroll[0]["y"] if scroll else None),
                    "singleSelectGroups": groups,
                })

            has_runtime = any(recs or scroll for recs, scroll in all_records.values())
            if has_runtime:
                (OUT / "assets").mkdir(parents=True, exist_ok=True)
                (OUT / "assets" / "spa-runtime.js").write_text(RUNTIME)

            for route in routes:
                recs, scroll = all_records[route]
                print(f"- capturing {route} -> {route_to_file(route)}")
                capture(page, base_url, route, routemap, has_runtime, recs, scroll,
                        OUT / route_to_file(route))
            browser.close()

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
                print("- gate -1b: recorded behaviour replays on the static page")
                behaved = behavior_gate(routes, static_url)
                print("- gate -1: running app vs static capture")
                pixels = parity_gate(routes, base_url, static_url)
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
        print("\nGATE FAILED — the static capture is not the running app. Do not proceed to stage 0.",
              file=sys.stderr)
        sys.exit(1)
    print("gate passed — this directory is now a valid stage 0 input")


if __name__ == "__main__":
    main()
