#!/usr/bin/env python3
"""The canonical Visual Edit editor smoke test — the operator checklist in
SKILL.md's stage 5 ("Editor smoke, by hand or Playwright") turned into one
script, so every conversion stops hand-rolling and re-discovering the same
contract. Written from a real conversion's post-mortem (skill-to-do.md #9):
"the skill required editor verification but shipped no tool".

  python3 smoke-editor.py --wp http://<site> --manifest conversion-manifest.json \\
      [--wp-cli 'docker exec <container> wp --allow-root'] [--admin user:pass] \\
      [--out smoke-editor-report]

Covers, end to end against a REAL WordPress + the real plugin UI:
  1. Text edit -> Save -> renders on the PUBLIC page -> a second, byte-
     identical save creates NO new history row (Clara_VE_History::record's
     own dedup, asserted via the same /clara-ve/v1/source + /history REST
     calls the editor itself makes, from inside the authenticated admin
     page's JS context — no separate nonce plumbing needed). Shared template
     parts on that canvas must carry zero page-source paths, including when
     WordPress renders the front-page pattern beside `.wp-site-blocks`.
  2. Every ordinary converted Page key: the editor opens it, the bridge
     stamps at least one `data-cve-path`, and a click exposes an editable
     text target. This is read-only: it never types or saves. It catches a
     root selector that assumes `.wp-block-post-content` remains inside
     `.wp-site-blocks` after a template part, while WordPress renders it as
     a sibling under `main` (FinProX, 2026-08-02).
  3. Every chrome part the theme's `inc/visual-edit.php` contract declares
     (majority `header`/`footer` ALWAYS, plus every entry in the
     generated `$contract['parts']` — the header-2/footer-2/... variants):
     the editor opens it and the bridge stamps at least one
     `data-cve-path`. Zero paths is the exact, previously-undetected
     symptom of a `wp:template-part` missing `tagName` — the wrapper comes
     out as a bare `<div>` and the plugin's root selectors
     (`header.wp-block-template-part`) find nothing (skill-to-do.md #3).
     A real text edit is also attempted on each part, not just a path
     count, and confirmed on that part's own public preview page.
  4. Every nav entry the manifest declares: the zone exists in the live
     DOM; with --wp-cli, one item's title is changed via wp-cli, confirmed
     on the PUBLIC page, then restored — C4 (verify-wp.py) proves wiring
     and consistency, only a live mutation proves propagation.
     It also clicks one front-page menu item in the real editor and requires
     the WordPress menu panel, even though shared header/footer parts carry
     no page-source paths on that canvas.
  5. The mobile drawer, IF the manifest's chrome/nav declares one: toggle
     click flips its declared open state (`aria-expanded` when present,
     otherwise its source's open class), the panel becomes visible, then
     closes.
  6. A connected form, end to end: connect (editor UI) -> the STORED
     SOURCE carries `[wp-form type="..."]` (never wait on the iframe to
     repaint after connecting — it does not necessarily repaint, see
     SKILL.md's gotchas) -> the PUBLIC page carries the plugin's real
     contract (hidden `name="form_id"`, hidden `name="clara_ve_nonce"`,
     action -> `/wp-json/clara-ve/v1/submit`) -> an ANONYMOUS submit in a
     separate, logged-out browser context creates a `clara_ve_submission`
     with the expected field values (DB-verified with --wp-cli, cleaned up
     afterwards) -> disconnect removes the `[wp-form]` marker and restores
     the plain form.

Discipline this script enforces on itself, per the same post-mortem
(skill-to-do.md #10 — timeouts that were both too long AND undiagnostic):
  - 10-20s timeouts for STRUCTURAL assumptions (an element exists, an
    iframe loaded, a status text appeared) — these fail fast or not at all.
  - Long timeouts (~60-180s) are reserved for genuinely slow network paths
    only (none of THIS script's own steps need one; the budget exists for
    future steps that do, e.g. an AI job).
  - On ANY failure: immediately print the screenshot path, the iframe URL
    (if one was open), the browser console errors collected so far, the
    live `data-cve-path` count, and the active editor `key` — never just
    "timeout".
  - Every completed step prints as it completes, unbuffered (`flush=True`
    everywhere, plus `-u`-safe): a hang is diagnosable from the last line
    printed, not from silence.

This machine has no WordPress to test against. Every Playwright-driven
assertion below is therefore VERIFIED-BY-CONSTRUCTION against the plugin's
own source (class-rest.php, class-forms.php, class-tokens.php, bridge.js,
editor.js — read in full while writing this) and exercised structurally
(argument parsing, the PHP contract regex extractor run against three real
generated `inc/visual-edit.php` files, the mobile-drawer/menu derivation
logic, `url_for`) but NOT run against a live install. Treat a first real
run as the first real test of the browser-driving code paths, and read
its report.json closely.

Exit code: 0 = every attempted check passed (checks skipped for a missing
--wp-cli/--admin are reported NOT RUN, never counted as passed or failed);
1 = at least one attempted check failed; 2 = WordPress was not reachable at
all, or login failed — nothing downstream could be attempted.
"""

import argparse, html, json, re, shlex, subprocess, sys, time, urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from PIL import Image, ImageChops

STRUCT_MS = 15_000   # structural assumptions: element exists, attribute flips, status text appears
LONG_MS = 60_000     # genuinely slow network paths (form submit round trip, save round trip)

# The same 0.6% gate A and gate -1 use. The editor is not allowed to be more
# different from the page than a conversion is allowed to be from its source:
# it is showing the very same document, so anything above this is a defect,
# not drift.
PARITY_THRESHOLD = 0.006

ap = argparse.ArgumentParser()
ap.add_argument("--wp", required=True)
ap.add_argument("--manifest", required=True)
ap.add_argument("--wp-cli", default="", dest="wp_cli",
                help="command prefix that runs wp-cli against the target site, e.g. "
                     "'docker exec clara-test-wp wp --allow-root'. Enables menu-item "
                     "mutation, form-submission DB verification + cleanup, and reading "
                     "the real min-seconds time-trap setting. Without it those checks "
                     "are reported NOT RUN, never counted as passed.")
ap.add_argument("--admin", default="", help="user:pass for wp-admin login. Without it "
                 "every admin-editor check (text edit, chrome parts, form connect) is "
                 "reported NOT RUN — only the public-page structural checks (nav zones "
                 "exist, mobile drawer) can run unauthenticated.")
ap.add_argument("--only-page-roots", action="store_true",
                help="run only the non-mutating ordinary-page edit-root check; requires --admin")
ap.add_argument("--out", default="smoke-editor-report")
args = ap.parse_args()

if args.only_page_roots and not args.admin:
    ap.error("--only-page-roots requires --admin")

WP = args.wp.rstrip("/")
MF = json.loads(Path(args.manifest).read_text())
OUT = Path(args.out).resolve()
OUT.mkdir(parents=True, exist_ok=True)

RUN_ID = str(int(time.time()))
report = {"wp": WP, "reachable": None, "loggedIn": None, "steps": {}}
console_errors = []


def log(msg):
    print(msg, flush=True)


_ARTICLE_URL_CACHE = {}


def _resolve_article_url(key):
    """An article page does NOT live at /{key}/ once the blog stage has
    run: it became a WordPress Post, and a Post's slug comes from its
    TITLE, not the manifest key (verify-wp.py's url_for carries the same
    note, matched off the built page's <h1>). This script has no --dist
    argument, so it matches off the manifest's own `title` field instead —
    "Anatomy of a smash burger — Caribbean Burgers Blog" split on the
    trailing separator, same fallback verify-wp.py uses when a page has no
    <h1> at all."""
    if key in _ARTICLE_URL_CACHE:
        return _ARTICLE_URL_CACHE[key]
    page_meta = next((p for p in MF.get("pages", []) if p.get("key") == key), None)
    if not page_meta:
        return None
    headline = re.split(r"\s+(?:\||-|—)\s+", str(page_meta.get("title", "")), maxsplit=1)[0].strip()
    if not headline:
        return None
    try:
        with urllib.request.urlopen(f"{WP}/wp-json/wp/v2/posts?per_page=100&status=publish", timeout=15) as resp:
            posts = json.loads(resp.read())
    except Exception:
        return None
    for p_ in posts:
        title = html.unescape(p_.get("title", {}).get("rendered", ""))
        if headline and (headline in title or title in headline):
            _ARTICLE_URL_CACHE[key] = p_.get("link")
            return p_.get("link")
    return None


def url_for(key):
    """Same derivation as verify-wp.py's url_for — kept in step, not
    reimplemented independently, or the two scripts would disagree about
    what a key's public address is."""
    if key == "front-page":
        return WP + "/"
    if key == "404":
        return f"{WP}/html2wp-404-preview-x9q/"
    page_meta = next((p for p in MF.get("pages", []) if p.get("key") == key), None)
    if page_meta and page_meta.get("kind") == "article":
        resolved = _resolve_article_url(key)
        if resolved:
            return resolved
    return f"{WP}/{key}/"


def editor_url(key):
    return f"{WP}/wp-admin/admin.php?page=visual-edit&key={key}"


def page_key_for_file(filename):
    for p in MF.get("pages", []):
        if p.get("file") == filename:
            return p.get("key", Path(filename).stem)
    return Path(filename).stem


def dump_failure(step, page=None, frame=None, extra=None):
    """The mandated failure dump: screenshot, iframe URL, console errors,
    data-cve-path count, active key — printed immediately, never just
    "timeout"."""
    shot = OUT / f"failure-{step}-{RUN_ID}.png"
    iframe_url = None
    path_count = None
    active_key = None
    if page is not None:
        try:
            page.screenshot(path=str(shot), full_page=True)
        except Exception:
            shot = None
        try:
            active_key = re.search(r"[?&]key=([^&]+)", page.url)
            active_key = active_key.group(1) if active_key else None
        except Exception:
            pass
    if frame is not None:
        try:
            iframe_url = frame.url
        except Exception:
            pass
        try:
            path_count = frame.evaluate("document.querySelectorAll('[data-cve-path]').length")
        except Exception:
            pass
    log(f"  [FAIL] {step}")
    log(f"    screenshot: {shot}")
    log(f"    iframe url: {iframe_url}")
    log(f"    active key: {active_key}")
    log(f"    data-cve-path count: {path_count}")
    log(f"    console errors so far: {console_errors[-10:]}")
    if extra:
        log(f"    detail: {extra}")
    return {"ok": False, "screenshot": str(shot) if shot else None, "iframeUrl": iframe_url,
            "activeKey": active_key, "cvePathCount": path_count,
            "consoleErrors": console_errors[-10:], "detail": extra}


def wp_cli(php_or_args, timeout=20):
    """Run wp-cli with the caller-supplied prefix. `php_or_args` is either a
    raw PHP string (run via `wp eval`) or a list of wp-cli subcommand args."""
    if not args.wp_cli:
        return None
    prefix = shlex.split(args.wp_cli)
    if isinstance(php_or_args, str):
        cmd = prefix + ["eval", php_or_args]
    else:
        cmd = prefix + list(php_or_args)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": "wp-cli timed out"}
    if out.returncode != 0:
        return {"error": out.stderr.strip() or f"wp-cli exited {out.returncode}"}
    return out.stdout.strip()


def clear_form_rate_limits():
    """Clear only Visual Edit form-rate transients in a disposable WP smoke
    environment. The harness submits every declared form in one run, while
    the production endpoint correctly permits one request per IP per minute;
    without this scoped cleanup, form two tests the throttle rather than its
    own connection contract. No-op when --wp-cli is unavailable."""
    if not args.wp_cli:
        return None
    # BOTH limiters. The plugin's is one request per IP per minute; the
    # STANDALONE THEME has its own — five per ten minutes, in
    # `html2wp_form_<md5(ip)>` — and it is the one that bites now that public
    # behaviour belongs to the theme. Clearing only the plugin's left every
    # form after the first testing the throttle, and a re-run within ten
    # minutes testing it from the first form on: three forms reported 429 and
    # the submit path went unexercised while the step still called itself
    # green.
    return wp_cli(
        "global $wpdb; $n = 0; "
        "foreach (array('_transient_clara_ve_form_rl_', '_transient_timeout_clara_ve_form_rl_', "
        "'_transient_html2wp_form_', '_transient_timeout_html2wp_form_') as $prefix) { "
        "$like = $wpdb->esc_like($prefix) . '%'; "
        "$n += (int) $wpdb->query($wpdb->prepare(\"DELETE FROM {$wpdb->options} WHERE option_name LIKE %s\", $like)); } "
        "echo $n;"
    )


# ---------------------------------------------------------------------------
# Theme contract: read inc/visual-edit.php for the declared chrome `parts`
# and `menus`. No --theme-dir flag is taken (the team's other scripts don't
# take one either) — the path is derived the same way every stage of this
# skill derives it: {workspace}/theme/{slug}/inc/visual-edit.php.
# A light regex extractor, not a PHP parser: make-theme.mjs emits this file
# in one very regular shape (verified against three real generated files —
# kinto, lumen, drevodom-hron), one `array( 'key' => '...', ... )` literal
# per line. If that shape ever changes this degrades to an empty list with
# a printed warning, never a crash.
# ---------------------------------------------------------------------------

def find_contract_file():
    ws = MF.get("workspace")
    slug = (MF.get("site") or {}).get("slug")
    if ws and slug:
        p = Path(ws) / "theme" / slug / "inc" / "visual-edit.php"
        if p.exists():
            return p
    if ws:
        hits = list(Path(ws).glob("theme/*/inc/visual-edit.php"))
        if hits:
            return hits[0]
    return None


def extract_array_block(text, key):
    m = re.search(re.escape(f"$contract['{key}']") + r"\s*=\s*array\((.*?)\n\t\);", text, re.S)
    return m.group(1) if m else ""


def parse_contract(path):
    parts, menus = [], []
    if not path:
        return parts, menus
    text = path.read_text()
    parts_block = extract_array_block(text, "parts")
    for m in re.finditer(
        r"'key'\s*=>\s*'([^']*)'.*?'area'\s*=>\s*'([^']*)'.*?'label'\s*=>\s*'([^']*)'.*?'preview_key'\s*=>\s*'([^']*)'",
        parts_block,
    ):
        parts.append({"key": m.group(1), "area": m.group(2), "label": m.group(3), "previewKey": m.group(4)})
    menus_block = extract_array_block(text, "menus")
    for m in re.finditer(
        r"'location'\s*=>\s*'([^']*)'.*?'selector'\s*=>\s*'([^']*)'.*?'label'\s*=>\s*'([^']*)'",
        menus_block,
    ):
        menus.append({"location": m.group(1), "selector": m.group(2), "label": m.group(3)})
    return parts, menus


# ---------------------------------------------------------------------------
# Mobile drawer detection: the manifest schema (assets/MANIFEST.md) has no
# dedicated boolean for this — it shows up as a `chrome.trailing[]` entry
# whose component name says so, or a `nav[]` entry whose own label says so
# (the sample schema's own example: "Main navigation (drawer)"). Heuristic,
# not a hard field — reported as such.
# ---------------------------------------------------------------------------

def detect_drawer():
    for t in (MF.get("chrome") or {}).get("trailing", []):
        if re.search(r"drawer", t.get("component", ""), re.I):
            return True, f"chrome.trailing component '{t.get('component')}'"
    for n in MF.get("nav", []):
        if re.search(r"drawer|mobile", n.get("label", ""), re.I):
            return True, f"nav entry '{n.get('label')}'"
    return False, None


# ---------------------------------------------------------------------------
# Editor-page helpers shared by every admin-authenticated step.
# ---------------------------------------------------------------------------

def open_editor(page, key):
    page.goto(editor_url(key), timeout=STRUCT_MS)
    page.wait_for_selector("#clara-ve-frame", timeout=STRUCT_MS, state="attached")
    frame_el = page.wait_for_selector("#clara-ve-frame", timeout=STRUCT_MS)
    frame = frame_el.content_frame()
    if frame is None:
        raise RuntimeError("iframe#clara-ve-frame attached but content_frame() is None — not yet navigated")
    frame.wait_for_load_state("load", timeout=STRUCT_MS)
    return frame


def set_edit_mode(page, on=True):
    toggle = page.locator("#clara-ve-toggle")
    toggle.wait_for(state="visible", timeout=STRUCT_MS)
    pressed = toggle.get_attribute("aria-pressed") == "true"
    if pressed != on:
        toggle.click()
        page.wait_for_function(
            "(want) => document.getElementById('clara-ve-toggle').getAttribute('aria-pressed') === (want ? 'true' : 'false')",
            arg=on, timeout=STRUCT_MS,
        )


def cve_path_count(frame):
    return frame.evaluate("document.querySelectorAll('[data-cve-path]').length")


def wait_for_paths(frame, timeout_ms=STRUCT_MS):
    """Poll until the bridge has stamped its paths.

    open_editor() returns as soon as the iframe fires `load`, but bridge.js
    stamps data-cve-path AFTER that — so a single sample right there reads 0
    on a slower part and the caller concludes the part is wrapped in a bare
    <div> with no tagName. Verified live: the header reported "zero
    data-cve-path" while the very same failure dump, taken a moment later,
    counted 66 of them, and the footer's dump raced far enough to catch the
    frame at about:blank. That diagnostic sends someone hunting a
    theme.json/tagName bug that does not exist."""
    waited = 0
    while waited < timeout_ms:
        try:
            n = cve_path_count(frame)
        except Exception:
            n = 0
        if n:
            return n
        frame.page.wait_for_timeout(250)
        waited += 250
    return 0


def click_first_editable(frame, limit=15):
    """Try clicking each [data-cve-path] element (edit mode must already be
    on) until one becomes contenteditable (bridge.js's startEdit() marker,
    see assets/bridge.js). Returns the locator that worked, or None. No
    assumption about which attribute means "text" beyond what bridge.js
    itself sets — this is the same generic contract materialize-js-text.py
    and the rest of the skill treat data-cve-path as."""
    # Candidates in "most likely to be editable text" order, not raw DOM
    # order. Raw order is the wrong order on any modern layout: the first
    # dozen [data-cve-path] elements are the page's structural wrappers, and
    # the plugin starts an edit on a TEXT element. Verified live on a page
    # with 314 stamped paths and plenty of editable copy — the first 15 were
    # all container <div>s, so this returned None and the step reported the
    # editor broken. A text LEAF (no element children), with real text and a
    # real box, is what a person clicks; managed nav zones are excluded
    # because those open the menu panel by design, not an inline edit.
    paths = frame.evaluate("""() => {
      const all = [...document.querySelectorAll('[data-cve-path]')];
      const score = (e) => {
        if (e.closest('[data-ve-nav]')) return -1;
        const t = (e.textContent || '').trim();
        if (t.length < 3) return -1;
        const r = e.getBoundingClientRect();
        if (r.width < 20 || r.height < 8) return -1;
        return e.children.length === 0 ? 2 : (e.children.length <= 2 ? 1 : 0);
      };
      return all.map((e) => ({ p: e.getAttribute('data-cve-path'), s: score(e) }))
                .filter((x) => x.s >= 0)
                .sort((a, b) => b.s - a.s)
                .map((x) => x.p);
    }""")
    if not paths:
        paths = frame.evaluate(
            "() => [...document.querySelectorAll('[data-cve-path]')].map((e) => e.getAttribute('data-cve-path'))")
    for path in paths[:limit]:
        el = frame.locator(f'[data-cve-path="{path}"]').first
        try:
            el.click(timeout=2000)
        except Exception:
            continue
        try:
            if el.get_attribute("contenteditable") == "plaintext-only":
                return el
        except Exception:
            continue
        # Not a text/link target (e.g. it opened a zone panel instead) —
        # close whatever that opened before trying the next candidate.
        try:
            frame.page.keyboard.press("Escape")
        except Exception:
            pass
    return None


def part_own_text_leaves(frame, area):
    """How many TEXT LEAVES this chrome part owns, ignoring managed nav zones.

    A header of logo + hamburger + menu has none: every word in it belongs to
    the nav zone, which opens the MENU panel by design and never an inline
    edit. That is a property of the DESIGN, not a broken editor — but
    click_first_editable() returns None either way, so the step used to report
    the part unreachable and fail a site whose header is the most ordinary
    header there is (verified on above: six header parts, 23 stamped paths
    each, all correctly scoped, and not one word of their own).

    Counted over the part's OWN DOM region rather than over stamped elements,
    because "nothing stamped" and "nothing to stamp" are the two cases that
    must not be confused: a part whose text leaves exist but were never
    stamped is still a failure, and this returns non-zero for it. Same leaf
    test click_first_editable() scores with, so the two cannot disagree about
    what counts as text."""
    return frame.evaluate("""(area) => {
      // hotfix (creative-011): the template part FIRST, and the bare tag only
      // when the page renders no part of that area. The union selector matched
      // every <header>/<footer> on the preview page, and a blog card is
      // <article><a><img></a><div><header><h2>… — so the card headlines on the
      // listing (and on the front page's blog strip) were counted as text the
      // HEADER PART owns. A header of logo + managed menu then reported
      // "N text leaf(s) … but none became editable" and failed a part whose
      // own text is genuinely zero.
      const partSel = area === 'header' ? 'header.wp-block-template-part'
                    : area === 'footer' ? 'footer.wp-block-template-part' : 'main';
      const bareSel = area === 'header' ? 'header' : area === 'footer' ? 'footer' : 'main';
      let roots = document.querySelectorAll(partSel);
      if (!roots.length) roots = document.querySelectorAll(bareSel);
      let n = 0;
      for (const root of roots) {
        for (const el of root.querySelectorAll('*')) {
          if (el.children.length) continue;
          if (el.closest('[data-ve-nav]')) continue;
          if ((el.textContent || '').trim().length < 3) continue;
          const r = el.getBoundingClientRect();
          if (r.width < 20 || r.height < 8) continue;
          n++;
        }
      }
      return n;
    }""", area)


def save_and_wait(page):
    save_btn = page.locator("#clara-ve-save")
    save_btn.click(timeout=STRUCT_MS)
    page.wait_for_function(
        "() => { const s = document.getElementById('clara-ve-status'); return s && /Saved|Error/.test(s.textContent || ''); }",
        timeout=LONG_MS,
    )
    return page.locator("#clara-ve-status").inner_text()


def replace_text_and_restore(page, target, marker, key):
    """Type `marker` over the element's whole text, returning a callable that
    puts the original text back and saves.

    Two things this fixes. `Control+A` inside a contenteditable does NOT
    reliably select only that element's contents — verified live, the marker
    was PREPENDED and the site shipped
    "CVE-SMOKE-1785428135Immersive Sound, Simplified" on its home page. A
    range selection over the element is exact.

    And the edit has to be UNDONE. This test writes into the real content of a
    real site through the real save path; leaving its markers behind means the
    conversion it was run to verify is the thing it damaged. The front page
    came out 52px taller for exactly that reason, which then read as a
    conversion defect in the visual review."""
    original = target.evaluate("(el) => el.textContent")
    source_before = rest_get(page, f"/clara-ve/v1/source?key={key}")
    original_source = source_before.get("source", "") if isinstance(source_before, dict) else str(source_before)
    original_pseudo = source_before.get("pseudo", []) if isinstance(source_before, dict) else []

    def put(text):
        target.evaluate("""(el, t) => {
          const r = document.createRange();
          r.selectNodeContents(el);
          const sel = el.ownerDocument.getSelection();
          sel.removeAllRanges(); sel.addRange(r);
        }""", text)
        target.type(text)
        target.press("Enter")
        return save_and_wait(page)

    status = put(marker)

    def restore():
        try:
            # Restore the exact pre-test document through the plugin's own
            # source endpoint. Re-clicking the edited node is fragile after
            # Save: a parent-frame style panel can cover it, and some bridges
            # have already torn down contenteditable so typing creates no
            # dirty state (Save stays disabled). The REST endpoint is the
            # editor's real persistence path, records history, syncs the
            # render target, and lets us prove byte identity afterwards.
            rest_post(page, "/clara-ve/v1/source", {
                "key": key, "source": original_source, "pseudo": original_pseudo,
            })
            after = rest_get(page, f"/clara-ve/v1/source?key={key}")
            after_source = after.get("source", "") if isinstance(after, dict) else str(after)
            return after_source == original_source and marker not in after_source
        except Exception as e:
            log(f"  WARNING: could not restore original text ({e}) — the site may still carry a test marker")
            return False

    return original, status, restore


def rest_get(page, path):
    return page.evaluate("(p) => window.wp.apiFetch({ path: p })", path)


def rest_post(page, path, data):
    return page.evaluate(
        "(a) => window.wp.apiFetch({ path: a[0], method: 'POST', data: a[1] })",
        [path, data],
    )


# ---------------------------------------------------------------------------
# Step 0 — reachability + login
# ---------------------------------------------------------------------------

def step_preflight(browser):
    ctx = browser.new_context()
    page = ctx.new_page()
    try:
        resp = page.goto(WP + "/", timeout=STRUCT_MS)
        report["reachable"] = bool(resp and resp.status < 500)
        log(f"preflight: {WP}/ -> {resp.status if resp else 'no response'}")
    except PWTimeout:
        report["reachable"] = False
        log(f"preflight: {WP}/ did not respond within {STRUCT_MS}ms")
    except Exception as e:
        report["reachable"] = False
        log(f"preflight: {WP}/ unreachable — {e}")
    ctx.close()
    return report["reachable"]


def step_login(browser):
    if not args.admin:
        log("login: SKIPPED — no --admin given, admin-editor checks will be NOT RUN")
        report["loggedIn"] = None
        return None
    user, _, pw = args.admin.partition(":")
    ctx = browser.new_context()
    page = ctx.new_page()
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    try:
        page.goto(WP + "/wp-login.php", timeout=STRUCT_MS)
        page.fill("#user_login", user, timeout=STRUCT_MS)
        page.fill("#user_pass", pw, timeout=STRUCT_MS)
        page.click("#wp-submit", timeout=STRUCT_MS)
        page.wait_for_selector("#wpadminbar", timeout=STRUCT_MS)
        report["loggedIn"] = True
        log("login: OK")
        return ctx, page
    except Exception as e:
        report["loggedIn"] = False
        report["steps"]["login"] = dump_failure("login", page)
        log(f"login: FAILED — {e}")
        ctx.close()
        return None


# ---------------------------------------------------------------------------
# Step 1 — text edit -> save -> renders live -> identical save = no new revision
# ---------------------------------------------------------------------------

def step_text_edit_and_idempotent_save(page):
    step = "textEditIdempotentSave"
    key = "front-page"
    try:
        frame = open_editor(page, key)
    except Exception as e:
        report["steps"][step] = dump_failure(step, page, extra=str(e))
        return
    path_count = cve_path_count(frame)
    log(f"{step}: opened key={key}, {path_count} data-cve-path element(s) in iframe")
    if path_count == 0:
        report["steps"][step] = dump_failure(step, page, frame, "zero data-cve-path in front-page — nothing to edit")
        return
    shared_part_paths = frame.evaluate(
        "document.querySelectorAll('.wp-block-template-part [data-cve-path]').length"
    )
    if shared_part_paths:
        report["steps"][step] = dump_failure(
            step,
            page,
            frame,
            f"{shared_part_paths} page-source path(s) leaked into shared template parts",
        )
        return

    set_edit_mode(page, True)
    marker = f"CVE-SMOKE-{RUN_ID}"
    target = click_first_editable(frame)
    if target is None:
        report["steps"][step] = dump_failure(step, page, frame, "no [data-cve-path] element became contenteditable after clicking up to 15 candidates")
        return
    restore_text = None
    try:
        _orig, status, restore_text = replace_text_and_restore(page, target, marker, key)
    except Exception as e:
        report["steps"][step] = dump_failure(step, page, frame, f"could not type into contenteditable target: {e}")
        return

    try:
        pass
    except Exception as e:
        report["steps"][step] = dump_failure(step, page, frame, f"Save did not reach a terminal status in {LONG_MS}ms: {e}")
        return
    if "Saved" not in status:
        report["steps"][step] = dump_failure(step, page, frame, f"save status text was '{status}', expected 'Saved ✓'")
        return
    log(f"{step}: saved, status='{status}'")

    # Renders live on the public page.
    pub_ok = False
    pub = page.context.new_page()
    try:
        for _ in range(int(STRUCT_MS / 1000)):
            pub.goto(url_for(key), timeout=STRUCT_MS)
            if marker in pub.content():
                pub_ok = True
                break
            pub.wait_for_timeout(1000)
    except Exception:
        pass
    if not pub_ok:
        report["steps"][step] = dump_failure(step, page, frame, f"marker '{marker}' never appeared on {url_for(key)}")
        pub.close()
        if restore_text:
            restore_text()
        return
    log(f"{step}: marker confirmed live on {url_for(key)}")

    # Second, byte-identical save must not append a new history row — asked
    # via the exact REST path the editor itself uses, from inside the
    # authenticated admin page, so no separate nonce handling is needed.
    try:
        before = rest_get(page, f"/clara-ve/v1/history?key={key}")
        current_source = rest_get(page, f"/clara-ve/v1/source?key={key}")
        head_before = before[0]["id"] if before else None
        rest_post(page, "/clara-ve/v1/source", {"key": key, "source": current_source.get("source", current_source) if isinstance(current_source, dict) else current_source, "pseudo": []})
        after = rest_get(page, f"/clara-ve/v1/history?key={key}")
        head_after = after[0]["id"] if after else None
    except Exception as e:
        report["steps"][step] = dump_failure(step, page, frame, f"REST history/source round trip failed: {e}")
        pub.close()
        return
    pub.close()
    if head_before != head_after:
        if restore_text:
            restore_text()
        report["steps"][step] = {"ok": False, "detail": f"identical re-save appended a new history row: head {head_before} -> {head_after}"}
        log(f"{step}: FAIL — identical save created a new history row ({head_before} -> {head_after})")
        return
    restored = restore_text() if restore_text else False
    if not restored:
        report["steps"][step] = dump_failure(step, page, frame, "original text could not be restored after the smoke edit")
        return
    source_after_restore = rest_get(page, f"/clara-ve/v1/source?key={key}")
    source_after_restore = source_after_restore.get("source", "") if isinstance(source_after_restore, dict) else str(source_after_restore)
    if marker in source_after_restore:
        report["steps"][step] = dump_failure(step, page, frame, "test marker remains in stored source after restore")
        return
    log(f"{step}: original text restored")
    report["steps"][step] = {"ok": True, "marker": marker, "publicUrl": url_for(key),
                             "historyHeadUnchanged": head_before, "textRestored": restored,
                             "sharedTemplatePartPaths": shared_part_paths}
    log(f"{step}: PASS — identical save left history head at {head_before}")


# ---------------------------------------------------------------------------
# Step 2 — every ordinary converted Page gets a real editable target without
# writing. A non-front-page root that assumes all template output is below
# .wp-site-blocks misses valid WordPress layouts whose `main` is a sibling of
# that wrapper after a template part.
# ---------------------------------------------------------------------------

def step_page_edit_roots(page):
    step = "pageEditRoots"
    results = []
    for source_page in MF.get("pages", []):
        key = source_page.get("key", "")
        kind = source_page.get("kind", "page")
        if not key or key in ("front-page", "404") or kind == "article":
            continue
        log(f"{step}: checking page '{key}'")
        try:
            frame = open_editor(page, key)
        except Exception as e:
            results.append({"key": key, "ok": False,
                            "detail": dump_failure(f"{step}-{key}", page, extra=str(e))})
            continue
        count = wait_for_paths(frame)
        if count == 0:
            results.append({"key": key, "ok": False,
                            "detail": dump_failure(
                                f"{step}-{key}", page, frame,
                                "zero data-cve-path — the page content root did not match the bridge selector"
                            )})
            continue
        set_edit_mode(page, True)
        if click_first_editable(frame) is None:
            results.append({"key": key, "ok": False, "cvePathCount": count,
                            "detail": dump_failure(
                                f"{step}-{key}", page, frame,
                                "stamped paths exist but no text target became editable after click"
                            )})
            continue
        results.append({"key": key, "ok": True, "cvePathCount": count})
        log(f"{step}: PASS '{key}' — {count} data-cve-path, editable text target opened")
    report["steps"][step] = {"ok": all(r["ok"] for r in results), "pages": results}


# ---------------------------------------------------------------------------
# Step 3 — every declared chrome part gets a real data-cve-path AND a
# propagated edit, not just a path count.
# ---------------------------------------------------------------------------

def _part_preview_key(taken=()):
    """The key of a page that renders the shared chrome parts.

    A consensus subpage always does; the front page does not (see below).
    Falls back to "front-page" only for a site that has no other page.

    `taken` is the set of keys the theme contract already claims as some
    VARIANT's preview page. A page that renders header-2 does not render
    `header`, so reading the majority header's marker back from it can only
    ever fail — verified live on ai-saas, where every consensus subpage owns
    its own header variant and the majority header belongs to the front page
    alone. Excluding the claimed keys leaves exactly the pages the majority
    part is used on, which is what this has to return.
    """
    # hotfix (playful-009): an ARTICLE page is not a page. It becomes a Post,
    # so there is no Page at its key at all, and templates/single.html carries
    # only the chrome its own article group used — on a site whose articles
    # have no footer, none. Verified on playful-marketing-aceternity: with
    # pricing and blog both claimed by footer variants, this returned
    # 'blog-post', the plugin previewed /blog/ (which renders footer-3), and
    # the majority footer failed with "Element has nested markup" — a real part
    # reported as broken because it was read back off the wrong page.
    for p in MF.get("pages", []):
        if (p.get("chrome") == "consensus" and p.get("kind") != "article"
                and p.get("key") != "front-page" and p.get("key") not in taken):
            return p["key"]
    # Still nothing: every non-article Page is claimed by its own variant —
    # a real site shape, not an edge case (a site with exactly one ordinary
    # subpage plus a blog). The majority part can still be used exclusively
    # by posts (templates/single.html), and url_for() now resolves an
    # article key to its REAL post permalink via the REST API (matched off
    # the manifest's title field) rather than guessing /{key}/ — the
    # guessed-URL failure playful-009 hit is closed, so an article
    # candidate is safe here PROVIDED resolution actually succeeds; one
    # that doesn't resolve is worse than the front-page fallback below, so
    # it is skipped rather than returned.
    for p in MF.get("pages", []):
        if (p.get("chrome") == "consensus" and p.get("kind") == "article"
                and p.get("key") not in taken and _resolve_article_url(p["key"])):
            return p["key"]
    # Nothing left but the front page. It is a legitimate answer — a site
    # whose front-page template references the shared parts (frontOwnsFooter
    # false, chrome cut from the pattern) renders them there like any other
    # page — and if it does not, the step fails loudly, which is correct:
    # a part no page renders is a part the owner cannot preview.
    return "front-page"


# The one implementation of "is this stamping scoped to its key", shared with
# test-region-scope.sh so the live check and its fixtures cannot drift apart.
REGION_SCOPE_JS = (Path(__file__).parent / "lib" / "region-scope.js").read_text()


def step_media_reachable(page):
    """Every prominent image must answer a click with the IMAGE panel.

    This exists because a whole class of defect was invisible to every other
    check here and in the gates. The near-universal hero treatment lays an
    empty tinting div over the photograph — `<div class="absolute inset-0
    bg-foreground/35">` — and that div is stamped, empty, and ABOVE the image
    in the hit stack. Every click on the hero then opened the overlay's panel
    and the photograph could not be selected at all. Verified live on a
    converted wedding site: the front page and all seven hero subpages.

    Nothing caught it. Pixel gates matched (the overlay is part of the
    design), C2c only requires that SOME editable text target opens, and the
    text-edit steps above never touch an image. The owner discovers it by
    trying to change the picture — which is the first thing anyone does to a
    hero. So: click the image, and require what gets selected to BE the
    image.
    """
    step = "mediaReachable"
    results = []
    for source_page in MF.get("pages", []):
        key = source_page.get("key", "")
        kind = source_page.get("kind", "page")
        if not key or kind == "article":
            continue
        try:
            frame = open_editor(page, key)
        except Exception as e:
            results.append({"key": key, "ok": False,
                            "detail": dump_failure(f"{step}-{key}", page, extra=str(e))})
            continue
        if wait_for_paths(frame) == 0:
            continue
        set_edit_mode(page, True)
        # The largest image on the page, clicked off-centre so a centred
        # caption above it cannot be what receives the click.
        # hotfix (playful-008): the coordinates must be relative to the IMAGE,
        # and the click must go to the image's own locator. The preview iframe
        # renders at FULL CONTENT HEIGHT — this file's own header says so — so a
        # viewport-relative point taken off getBoundingClientRect() and handed
        # to body.click() is a point Playwright then tries to reach by
        # scrolling the ADMIN page. On a page whose main image sits 3000px down
        # that never lands: "element is not stable", then wp-admin's own
        # Appearance menu intercepts the pointer, then a 30s Locator.click
        # timeout escapes as an unhandled exception and the whole run dies
        # BEFORE chromeParts, menus, the drawer and the form step. Verified on
        # playful-marketing-aceternity: front-page (image near the top) passed,
        # pricing (deco image at y≈3000) aborted the run.
        # The candidate must be an image a VISITOR could click. Two disqualify
        # it, and neither is the defect this step hunts: a negative z-index
        # (painted behind its own section — the ordinary shape of a decorative
        # background) and a click point outside the document box (a deco panel
        # bled off the left edge). Taking the largest image unconditionally
        # picked exactly that on playful-marketing-aceternity's pricing page —
        # a 531x551 Social_Media.svg at left:-153, z-index:-10 — whose 18%
        # point is x=-58, a coordinate no click can reach. An image that IS
        # reachable but answers with an overlay must still FAIL, so nothing
        # here consults elementFromPoint; that is the finding, not a skip.
        spot = frame.locator("body").evaluate("""() => {
          const docW = document.documentElement.scrollWidth;
          const cands = [...document.querySelectorAll('img')]
            .map((i) => ({ i, r: i.getBoundingClientRect() }))
            .filter(({ i, r }) => r.width > 400 && r.height > 200
              && (parseInt(getComputedStyle(i).zIndex, 10) || 0) >= 0)
            .sort((a, b) => (b.r.width * b.r.height) - (a.r.width * a.r.height));
          for (const { i, r } of cands) {
            const x = Math.round(r.left + r.width * 0.18);
            const y = Math.round(r.top + r.height * 0.35);
            if (x < 0 || y < 0 || x > docW) continue;
            document.querySelectorAll('[data-cve-smoke-media]').forEach((e) => e.removeAttribute('data-cve-smoke-media'));
            i.setAttribute('data-cve-smoke-media', '1');
            return { x: Math.round(r.width * 0.18), y: Math.round(r.height * 0.35) };
          }
          return null;
        }""")
        if not spot:
            results.append({"key": key, "ok": True, "skipped": "no prominent image"})
            continue
        # hotfix (tidy-015): freeze transitions in the PREVIEW before clicking.
        # The card idiom every Tailwind template ships — `group-hover:scale-105
        # transition duration-700` on the image — means the pointer Playwright
        # moves onto the image starts a 700ms scale, and its actionability wait
        # ("element is visible, enabled and STABLE") re-checks while the element
        # is still growing, backs the pointer off, and the image springs back:
        # the two states alternate until the 30s timeout. Verified on tidy's
        # blog listing (news-single.jpg, exactly that class list) — 58 stability
        # retries, then an escaped TimeoutError that killed the run before
        # chromeParts, menus, the drawer and the forms step, the same abort
        # shape playful-008 fixed for a different trigger. Killing transitions
        # keeps every real actionability check (visible, enabled, hit-testable)
        # and removes only the self-inflicted motion.
        frame.locator("body").evaluate("""() => {
          const s = document.createElement('style');
          s.id = 'cve-smoke-freeze';
          s.textContent = '*,*::before,*::after{transition:none!important;animation:none!important}';
          document.head.appendChild(s);
        }""")
        target = frame.locator('[data-cve-smoke-media="1"]')
        try:
            target.click(position={"x": spot["x"], "y": spot["y"]})
        except Exception as e:
            # hotfix (bench013new): "Playwright would not let me click" is not
            # the same fact as "an overlay answered the click", and only the
            # second is this step's finding. Playwright HOVERS the element as
            # part of clicking it, and the near-universal thumbnail idiom is an
            # absolutely-positioned overlay that appears ON HOVER
            # (`.item-thumbs:hover .hover-wrap{opacity:1;transform:scale(1)}` —
            # a fancybox gallery, a lightbox caption, a "view project" veil).
            # Approaching the image is what summons the thing that then
            # intercepts the pointer, so actionability never settles and the
            # step fails on a page where a real user's click reaches the image
            # perfectly. Verified live on a Bootstrap 3 portfolio grid: the
            # locator click timed out on span.overlay-img, and a point click at
            # the same spot selected the <img> with kind="image".
            #
            # So retry with force=True — a REAL mouse click at the same point,
            # routed by the browser to whatever is topmost there, exactly as a
            # visitor's click is. The assertion below is unchanged and still
            # does the work: if the overlay genuinely answers, it is what
            # carries data-cve-selected and the step still FAILS.
            try:
                target.click(position={"x": spot["x"], "y": spot["y"]}, force=True)
            except Exception as e2:
                # A click this step cannot land is this step's own finding, never a
                # reason for the six checks after it to go unrun.
                results.append({"key": key, "ok": False,
                                "detail": dump_failure(f"{step}-{key}-click", page,
                                                       extra=f"{e}\n--- forced retry also failed ---\n{e2}")})
                continue
        page.wait_for_timeout(700)
        sel = frame.locator("body").evaluate("""() => {
          const el = document.querySelector('[data-cve-selected]');
          return el ? { tag: el.tagName.toLowerCase(), kind: el.getAttribute('data-cve-kind'),
                        cls: (el.className || '').toString().slice(0, 60) } : null;
        }""")
        ok = bool(sel and sel.get("kind") == "image")
        if not ok and sel and sel.get("kind") == "text":
            # A hero is a full-bleed photograph with the site's name centred on
            # it, so the CENTRE of the largest image is the wordmark — and
            # selecting the words you clicked is correct, not a defect. What
            # this step exists to prove is that the PHOTOGRAPH is reachable at
            # all (the decorative-overlay bug, where an empty tinting div
            # answered every click anywhere on the image). So try once more
            # away from the middle, where a centred title cannot be, and only
            # then call it unreachable.
            try:
                box = target.bounding_box()
                if box:
                    target.click(position={"x": max(4.0, box["width"] * 0.12),
                                           "y": max(4.0, box["height"] * 0.12)}, force=True)
                    page.wait_for_timeout(700)
                    sel2 = frame.locator("body").evaluate("""() => {
                      const el = document.querySelector('[data-cve-selected]');
                      return el ? { tag: el.tagName.toLowerCase(), kind: el.getAttribute('data-cve-kind'),
                                    cls: (el.className || '').toString().slice(0, 60) } : null;
                    }""")
                    if sel2 and sel2.get("kind") == "image":
                        log(f"{step}: PASS '{key}' — centre is a headline over the photo; "
                            f"the image selects off-centre")
                        results.append({"key": key, "ok": True, "selected": sel2,
                                        "note": "centre of the image carries centred text "
                                                f"(<{sel['tag']} class=\"{sel['cls']}\">); "
                                                "clicked off-centre instead"})
                        continue
            except Exception:
                pass
        if ok:
            log(f"{step}: PASS '{key}' — clicking the image selects the image")
            results.append({"key": key, "ok": True, "selected": sel})
        else:
            got = f"<{sel['tag']} class=\"{sel['cls']}\">" if sel else "nothing"
            results.append({"key": key, "ok": False, "selected": sel,
                            "detail": dump_failure(
                                f"{step}-{key}", page, frame,
                                f"clicking the page's main image selected {got} instead of the image — "
                                "a decorative element is covering it and answering the click"
                            )})
    bad = [r for r in results if not r.get("ok")]
    report["steps"][step] = {"ok": not bad, "pages": results}
    if bad:
        log(f"{step}: FAILED on {', '.join(r['key'] for r in bad)}")
    return not bad


def step_chrome_parts(page, parts):
    step = "chromeParts"
    # Always cover the majority header/footer (the ones NOT listed in
    # `parts` because they're the default), plus every declared variant —
    # SKILL.md's own checklist: "on every chrome variant part, not only the
    # majority pair".
    #
    # The canvas must be a page that RENDERS the part. The front page usually
    # is not one: templates/front-page.html references the page's own pattern,
    # which carries its inline chrome, so it holds no header part — and no
    # footer part either unless chrome.frontOwnsFooter is false. Reading the
    # marker back from the front page therefore fails on a perfectly good
    # part. Verified live on agenlabs: the footer edit propagated to all four
    # subpages while this step read the front page and reported a failure.
    # Per AREA: a variant's preview page renders THAT variant, so it cannot
    # also be where the majority part of the same area is read back from.
    claimed = {}
    for p in parts:
        claimed.setdefault(p.get("area") or "", set()).add(p.get("previewKey"))
    targets = [{"key": "header", "area": "header",
                "previewKey": _part_preview_key(claimed.get("header", set())), "label": "header (majority)"},
               {"key": "footer", "area": "footer",
                "previewKey": _part_preview_key(claimed.get("footer", set())), "label": "footer (majority)"}]
    front_key = _part_preview_key()
    for p in parts:
        targets.append({"key": p["key"], "area": p["area"], "previewKey": p["previewKey"] or front_key, "label": p["label"]})

    results = []
    for t in targets:
        log(f"{step}: checking part '{t['key']}' ({t['label']}), preview via key '{t['previewKey']}'")
        try:
            frame = open_editor(page, t["key"])
        except Exception as e:
            results.append({**t, "ok": False, "detail": dump_failure(f"{step}-{t['key']}", page, extra=str(e))})
            continue
        count = wait_for_paths(frame)
        # The count was never the question — the REGION was. Opening a variant
        # key used to load a page whose whole <main> was stamped, and a step
        # that only asked "is anything stamped?" called that a pass. Five
        # variant parts per site were certified editable while the editor was
        # showing the page's content (school-007, mc-011).
        scope = frame.evaluate(REGION_SCOPE_JS + f"({t['area']!r})")
        if count and not scope["ok"]:
            results.append({**t, "ok": False, "cvePathCount": count, "regionScope": scope,
                             "detail": dump_failure(f"{step}-{t['key']}", page, frame,
                                                     f"stamped region does not belong to key '{t['key']}': {scope['why']}")})
            continue
        if count == 0:
            results.append({**t, "ok": False,
                             "detail": dump_failure(f"{step}-{t['key']}", page, frame,
                                                     "zero data-cve-path — this part is very likely wrapped in a bare <div> "
                                                     "(missing wp:template-part tagName) rather than the tagged element the "
                                                     "plugin's root selectors expect")})
            continue

        set_edit_mode(page, True)
        marker = f"CVE-SMOKE-{t['key']}-{RUN_ID}"
        target = click_first_editable(frame)
        if target is None:
            # Two different things look identical from here, and only one is a
            # bug: the part HAS text the editor cannot open, or the part has no
            # text of its own at all. Ask the region before blaming the editor.
            leaves = part_own_text_leaves(frame, t["area"])
            if leaves == 0:
                log(f"{step}: PASS '{t['key']}' — {count} data-cve-path, correctly scoped; "
                    f"no text of its own to edit (logo/menu/image only)")
                results.append({**t, "ok": True, "cvePathCount": count, "regionScope": scope,
                                 "textTargets": 0,
                                 "note": "part owns no text outside its managed nav zone — "
                                         "reachability proven by region scope and the menus step, "
                                         "no text edit attempted"})
                continue
            results.append({**t, "ok": False, "cvePathCount": count,
                             "detail": dump_failure(f"{step}-{t['key']}", page, frame,
                                                     f"{count} data-cve-path present and {leaves} text leaf(s) in the "
                                                     f"{t['area']} region, but none became editable")})
            continue
        restore_text = None
        try:
            _orig, status, restore_text = replace_text_and_restore(page, target, marker, t["key"])
        except Exception as e:
            results.append({**t, "ok": False, "cvePathCount": count,
                             "detail": dump_failure(f"{step}-{t['key']}", page, frame, str(e))})
            continue
        if "Saved" not in status:
            if restore_text:
                restore_text()
            results.append({**t, "ok": False, "cvePathCount": count,
                             "detail": dump_failure(f"{step}-{t['key']}", page, frame, f"status was '{status}'")})
            continue

        pub_ok = False
        pub = page.context.new_page()
        try:
            for _ in range(int(STRUCT_MS / 1000)):
                pub.goto(url_for(t["previewKey"]), timeout=STRUCT_MS)
                if marker in pub.content():
                    pub_ok = True
                    break
                pub.wait_for_timeout(1000)
        except Exception:
            pass
        pub.close()
        # Restore BEFORE reporting, on both paths: this test edits the real
        # content of the site it is verifying, so leaving its marker behind
        # damages the deliverable it just certified.
        restored = restore_text() if restore_text else False
        try:
            stored_after_restore = rest_get(page, f"/clara-ve/v1/source?key={t['key']}")
            stored_after_restore = stored_after_restore.get("source", "") if isinstance(stored_after_restore, dict) else str(stored_after_restore)
        except Exception:
            stored_after_restore = marker
        if not restored or marker in stored_after_restore:
            results.append({**t, "ok": False, "cvePathCount": count,
                             "detail": dump_failure(f"{step}-{t['key']}-restore", page, frame,
                                                     "original text was not restored cleanly; refusing to certify a site with smoke residue")})
            continue
        if not pub_ok:
            results.append({**t, "ok": False, "cvePathCount": count,
                             "detail": f"marker never appeared on {url_for(t['previewKey'])} — path count was {count}, save reported '{status}'"})
            continue

        results.append({**t, "ok": True, "cvePathCount": count, "textRestored": restored})
        log(f"{step}: PASS '{t['key']}' — {count} data-cve-path, edit confirmed and original text restored")

    report["steps"][step] = {"ok": all(r["ok"] for r in results), "parts": results}


# ---------------------------------------------------------------------------
# Step 3 — every declared nav zone exists; with --wp-cli, a mutation
# propagates and is restored.
# ---------------------------------------------------------------------------

def _retitle_php(menu, item, title):
    """PHP that changes a menu item's title and NOTHING else.

    wp_update_nav_menu_item() does not merge: it parses the given array over
    core's defaults and writes the result, so every field omitted is actively
    overwritten — type becomes 'custom', object-id 0, url '', and position 0,
    which core reads as "append" and recomputes to the END of the menu.
    A title-only call therefore unbinds the item from its page and moves it
    last, while wp-admin still shows the restored title so nothing looks
    wrong. Verified live: this test's own mutation check was corrupting the
    very menus it certified, and it reads as an intermittent import bug
    because the damage lands minutes after the import.
    """
    return (
        f"$it = get_post({item});"
        f"$cur = wp_setup_nav_menu_item($it);"
        f"$r = wp_update_nav_menu_item({menu}, {item}, array("
        f"  'menu-item-title'=>'{title}',"
        "   'menu-item-position'=>max(1,(int)$it->menu_order),"
        "   'menu-item-type'=>$cur->type,"
        "   'menu-item-object'=>$cur->object,"
        "   'menu-item-object-id'=>(int)$cur->object_id,"
        "   'menu-item-parent-id'=>(int)$cur->menu_item_parent,"
        "   'menu-item-target'=>$cur->target,"
        "   'menu-item-attr-title'=>$cur->attr_title,"
        "   'menu-item-description'=>$cur->description,"
        "   'menu-item-classes'=>implode(' ', (array)$cur->classes),"
        "   'menu-item-xfn'=>$cur->xfn,"
        "   'menu-item-status'=>'publish'"
        "   ) + ('custom' === $cur->type ? array('menu-item-url'=>$cur->url) : array()));"
    )


def step_menus(browser_page_public):
    step = "menus"
    nav_entries = MF.get("nav") or []
    if not nav_entries:
        report["steps"][step] = {"ok": True, "entries": [], "note": "manifest declares no nav groups"}
        log(f"{step}: manifest declares no nav — skipped")
        return

    prefix = (MF.get("site") or {}).get("prefix", "")
    front = url_for("front-page")
    sub_key = next((p["key"] for p in MF.get("pages", [])
                    if p.get("key") not in ("front-page", "404") and p.get("kind") != "article"), None)
    probes = [front] + ([url_for(sub_key)] if sub_key else [])

    results = []
    for i, entry in enumerate(nav_entries):
        loc = f"{prefix}_nav_{i + 1}"
        sel = entry.get("zoneSelector") or entry.get("selector", "")
        rec = {"location": loc, "selector": sel, "label": entry.get("label")}
        found_on = None
        for u in probes:
            try:
                browser_page_public.goto(u, timeout=STRUCT_MS)
                if browser_page_public.locator(sel).count() > 0:
                    found_on = u
                    break
            except Exception:
                continue
        if not found_on:
            rec["ok"] = False
            rec["detail"] = dump_failure(f"{step}-{loc}", browser_page_public, extra=f"selector '{sel}' matched nothing on {probes}")
            results.append(rec)
            continue
        rec["foundOn"] = found_on
        log(f"{step}: zone '{sel}' ({loc}) present on {found_on}")

        if not args.wp_cli:
            rec["ok"] = True
            rec["mutation"] = "NOT RUN — pass --wp-cli to assert a live edit propagates"
            results.append(rec)
            continue

        php = (
            "$locs = get_nav_menu_locations();"
            f"$id = isset($locs['{loc}']) ? (int) $locs['{loc}'] : 0;"
            "if (!$id) { echo 'NO_MENU'; exit; }"
            "$items = wp_get_nav_menu_items($id);"
            "if (!$items) { echo 'NO_ITEMS'; exit; }"
            "$it = $items[0];"
            "echo wp_json_encode(array('menu'=>$id,'item'=>$it->ID,'title'=>$it->title));"
        )
        out = wp_cli(php)
        if not out or out in ("NO_MENU", "NO_ITEMS") or (isinstance(out, dict) and "error" in out):
            rec["ok"] = False
            rec["detail"] = f"could not resolve a menu item for location '{loc}' via wp-cli: {out}"
            results.append(rec)
            continue
        try:
            info = json.loads(out)
        except ValueError:
            rec["ok"] = False
            rec["detail"] = f"wp-cli eval returned non-JSON: {out}"
            results.append(rec)
            continue

        new_title = f"CVE-SMOKE-{RUN_ID}"
        set_title = wp_cli(_retitle_php(info['menu'], info['item'], new_title)
                           + "echo is_wp_error($r) ? 'ERR' : 'OK';")
        propagated = False
        if set_title == "OK":
            try:
                browser_page_public.goto(found_on, timeout=STRUCT_MS)
                # text_content(), not inner_text(): inner_text is
                # VISIBILITY-AWARE and returns "" for a hidden element. Half
                # the menus on a real site live inside a closed overlay or
                # drawer — `aria-hidden="true"` until the header's toggle
                # opens it — so this read came back empty and three correctly
                # wired zones were reported as "did NOT propagate", with the
                # public page carrying the new title all along. What is being
                # asserted is that the THEME rendered the menu change into
                # the zone's markup; whether the zone is on screen at rest is
                # the design's business.
                propagated = new_title in (browser_page_public.locator(sel).text_content() or "")
            except Exception:
                propagated = False
        # Restore regardless of what we just observed — a smoke test must
        # never leave the site worse than it found it.
        restored_title = info["title"].replace("'", "\\'")
        wp_cli(_retitle_php(info['menu'], info['item'], restored_title))

        rec["ok"] = bool(propagated)
        rec["mutation"] = "propagated and restored" if propagated else "did NOT propagate to the public page (title was restored anyway)"
        if not propagated:
            rec["detail"] = dump_failure(f"{step}-{loc}-mutation", browser_page_public)
        results.append(rec)
        log(f"{step}: mutation on '{loc}' — {rec['mutation']}")

    report["steps"][step] = {"ok": all(r["ok"] for r in results), "entries": results}


# ---------------------------------------------------------------------------
# Step 4 — a front-page menu item opens its WordPress menu panel.
# ---------------------------------------------------------------------------

def step_front_menu_panel(admin_page):
    step = "frontMenuPanel"
    entries = MF.get("nav") or []
    if not entries:
        report["steps"][step] = {"ok": True, "note": "manifest declares no nav groups — skipped"}
        log(f"{step}: manifest declares no nav — skipped")
        return
    results = []
    try:
        frame = open_editor(admin_page, "front-page")
        set_edit_mode(admin_page, True)
        for entry in entries:
            selector = entry.get("zoneSelector") or entry.get("selector", "")
            rec = {"selector": selector, "label": entry.get("label")}
            if not selector:
                rec.update({"ok": False, "detail": "manifest nav entry has no selector"})
                results.append(rec)
                continue
            links = frame.locator(f"{selector} a")
            if links.count() == 0:
                rec.update({"ok": False, "detail": "declared menu zone has no links in the front-page preview"})
                results.append(rec)
                continue
            link = links.first
            if not link.is_visible():
                rec.update({"ok": None, "note": "zone exists but is hidden at the desktop editor width — not clicked"})
                results.append(rec)
                continue
            try:
                link.wait_for(state="visible", timeout=STRUCT_MS)
                link.click(timeout=STRUCT_MS)
                panel = admin_page.locator(".cve-panel")
                panel.wait_for(state="visible", timeout=STRUCT_MS)
                text = panel.inner_text()
                rec["ok"] = "MENU ITEM" in text
                rec["panel"] = "MENU ITEM" if rec["ok"] else text[:160]
                if not rec["ok"]:
                    rec["detail"] = dump_failure(step, admin_page, extra=f"clicking '{selector}' did not open the MENU ITEM panel")
                # Never leave a panel across the next selector: its z-index
                # can obscure the next menu link without changing the page.
                close = panel.locator(".cve-close")
                if close.count():
                    close.click(timeout=STRUCT_MS)
            except Exception as e:
                rec["ok"] = False
                rec["detail"] = dump_failure(step, admin_page, extra=f"{selector}: {e}")
            results.append(rec)
    except Exception as e:
        results.append({"ok": False, "detail": dump_failure(step, admin_page, extra=str(e))})
    clicked = [r for r in results if r.get("ok") is True]
    failed = [r for r in results if r.get("ok") is False]
    report["steps"][step] = {"ok": bool(clicked) and not failed, "entries": results,
                              "clickedVisibleZones": len(clicked)}
    log(f"{step}: {'PASS' if report['steps'][step]['ok'] else 'FAIL'} — {len(results)} declared menu zone(s)")


# ---------------------------------------------------------------------------
# Step 5 — mobile drawer, only if the manifest declares one.
# ---------------------------------------------------------------------------

def step_mobile_drawer(browser):
    step = "mobileDrawer"
    present, source = detect_drawer()
    if not present:
        report["steps"][step] = {"ok": True, "note": "not declared in manifest (no chrome.trailing drawer component, no nav entry labelled drawer/mobile) — skipped"}
        log(f"{step}: not declared — skipped")
        return

    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    try:
        page.goto(url_for("front-page"), timeout=STRUCT_MS)
        # Find the drawer toggle by what it MEANS, not by the first element
        # that happens to carry aria-expanded. That selector picks up an
        # accordion trigger on any page with an FAQ — verified live, where the
        # first match was a radix accordion button carrying aria-disabled, so
        # the step spent its whole timeout trying to click a disabled element
        # and reported the drawer broken while the real drawer worked fine.
        #
        # Ranked by accessible name (sr-only text / aria-label / title) rather
        # than by DOM position, and restricted to buttons that are actually
        # visible at this width. A drawer toggle also frequently has NO
        # aria-expanded until its handler runs once, so requiring the
        # attribute up front excludes the very element being looked for.
        handle = page.evaluate("""() => {
          const name = (b) => ((b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('title') || '')
              + ' ' + [...b.querySelectorAll('.sr-only,.screen-reader-text,.visually-hidden')]
                       .map((s) => s.textContent || '').join(' ')
              + ' ' + (b.textContent || '')).toLowerCase();
          const cands = [...document.querySelectorAll('button,[role="button"]')].filter((b) => {
            if (b.getAttribute('aria-disabled') === 'true' || b.disabled) return false;
            if (b.closest('[data-slot="accordion-trigger"]') || b.getAttribute('data-slot') === 'accordion-trigger') return false;
            const r = b.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          });
          const scored = cands.map((b) => {
            const n = name(b);
            let s = 0;
            if (/\\b(menu|navigation|nav)\\b/.test(n)) s += 3;
            if (b.closest('header')) s += 2;
            if (b.hasAttribute('aria-controls') || b.hasAttribute('aria-expanded')) s += 1;
            return { b, s };
          }).filter((x) => x.s >= 3).sort((a, b) => b.s - a.s);
          if (!scored.length) return null;
          scored[0].b.setAttribute('data-cve-smoke-drawer', '1');
          return true;
        }""")
        toggle = page.locator("[data-cve-smoke-drawer]").first if handle else page.locator("button[aria-expanded]").first
        toggle.wait_for(state="visible", timeout=STRUCT_MS)
        before = toggle.get_attribute("aria-expanded")
        before_class = toggle.get_attribute("class") or ""
        controls_id = toggle.get_attribute("aria-controls")
        panel = None
        if controls_id:
            candidate = page.locator(f"#{controls_id}")
            if candidate.count() > 0:
                panel = candidate
        if panel is None:
            candidate = page.locator("#mobile-menu, [id*='mobile-menu'], [class*='mobile-menu'], [class*='drawer']").first
            if candidate.count() > 0:
                panel = candidate
        panel_visible_before = panel.is_visible() if panel is not None else None
        toggle.click(timeout=STRUCT_MS)
        # A short settle rather than wait_for_function: the attribute is
        # flipped synchronously by the site's own click handler, so there is
        # nothing async to poll for — this just lets any transition finish
        # before the visibility check below reads computed style.
        page.wait_for_timeout(300)
        after = toggle.get_attribute("aria-expanded")
        after_class = toggle.get_attribute("class") or ""
        # Preserve sites that correctly expose ARIA, but do not reject an
        # untouched source whose own interaction contract is `.open`/
        # `.is-open`/`.active` plus a visible panel. Visual parity does not
        # justify rewriting the source just to satisfy the smoke assertion.
        class_open = lambda classes: bool(re.search(r"(?:^|\s)(?:open|is-open|active)(?:\s|$)", classes))
        aria_flipped = (after == "true") and (before != after)
        class_flipped = (not class_open(before_class)) and class_open(after_class)
        panel_visible = panel.is_visible() if panel is not None else None
        # The PANEL going from hidden to visible is the drawer opening, and on
        # a plain hand-written design it is the ONLY signal there is: the state
        # class lands on the panel (`#mobileMenu.open`), never on the button,
        # and the button carries no aria-expanded at all. Reading only the
        # toggle's own attribute and class reported a working drawer broken —
        # verified live on ai-saas, where the panel went display:none -> flex,
        # 0 -> 226px, on both the front page and a subpage.
        panel_flipped = panel_visible_before is False and panel_visible is True
        flipped = aria_flipped or class_flipped or panel_flipped
        if not flipped:
            report["steps"][step] = dump_failure(
                step, page,
                extra=f"drawer did not open: aria-expanded {before!r} -> {after!r}; "
                      f"class {before_class!r} -> {after_class!r}; "
                      f"panel visible {panel_visible_before!r} -> {panel_visible!r}"
            )
            ctx.close()
            return
        # Close it again — click the toggle a second time (a link-click-closes
        # variant is heuristic and skipped, since not every drawer's DOM
        # naming lets us find "a link inside the panel" reliably).
        toggle.click(timeout=STRUCT_MS)
        page.wait_for_timeout(300)
        closed = toggle.get_attribute("aria-expanded")
        closed_class = toggle.get_attribute("class") or ""
        panel_visible_after_close = panel.is_visible() if panel is not None else None
        aria_closed = closed == "false"
        class_closed = not class_open(closed_class)
        # Same asymmetry on the way back: with neither aria nor a class on the
        # toggle, "closed" is the panel being hidden again, which is exactly
        # what panel_visible_after_close already measures.
        closed_ok = (aria_closed if after is not None
                     else (class_closed if class_flipped else panel_visible_after_close is False)) \
            and panel_visible_after_close is not True
        report["steps"][step] = {
            "ok": flipped and panel_visible is not False and closed_ok,
            "source": source, "before": before, "afterOpen": after, "afterClose": closed,
            "beforeClass": before_class, "afterOpenClass": after_class, "afterCloseClass": closed_class,
            "panelVisibleBefore": panel_visible_before,
            "panelVisibleWhenOpen": panel_visible, "panelVisibleAfterClose": panel_visible_after_close,
        }
        log(f"{step}: {'PASS' if report['steps'][step]['ok'] else 'FAIL'} — aria-expanded {before} -> {after} -> {closed}; class {before_class!r} -> {after_class!r} -> {closed_class!r}")
    except Exception as e:
        report["steps"][step] = dump_failure(step, page, extra=str(e))
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# Step 5 — connect a form, submit it anonymously, verify the DB row,
# disconnect and confirm the source is restored.
# ---------------------------------------------------------------------------


def _diff_ratio(a_path, b_path):
    """Same measure as verify-static.py's diff_ratio — kept in step, not
    reimplemented independently, or the two scripts would disagree about what
    counts as a difference."""
    a, b = Image.open(a_path).convert("RGB"), Image.open(b_path).convert("RGB")
    if a.size != b.size:
        w = max(a.width, b.width); h = max(a.height, b.height)
        pa = Image.new("RGB", (w, h), (255, 0, 255)); pa.paste(a, (0, 0))
        pb = Image.new("RGB", (w, h), (255, 0, 255)); pb.paste(b, (0, 0))
        a, b = pa, pb
    d = ImageChops.difference(a, b).convert("L")
    return sum(d.histogram()[16:]) / (a.width * a.height)


def step_edit_preview_parity(browser, admin_page):
    """The editor must not change the design.

    A strict invariant, and the only check here that needs no reference to the
    original: whatever a visitor sees at rest, the edit preview must show the
    same. Any difference is a defect by definition, because the preview exists
    to show the page — not a version of it.

    This is the shape of bug that reached a delivered site. The token runtime
    wraps each hydrated zone in a marker div for the preview only. Its own
    docblock said that marker "is display:contents so it never affects
    layout"; nothing made that true, so in the preview the marker became the
    grid item and the real content stopped being one. A contact form at
    lg:col-span-7 inside a grid-cols-12 parent rendered 12px wide instead of
    660px — invisible. The blog's card grid collapsed the same way. Gate C2
    loaded an edit preview and asserted the bridge answered, which it did;
    nothing looked at the result.

    It loads the preview DIRECTLY (`?clara_edit=1&_clara_ve=<nonce>`) rather
    than through the editor's iframe. The first attempt screenshotted the
    iframe's <body> against a full-page capture of the public URL and every
    page failed, front page at 94% — two incomparable images, which is a
    worse gate than none. Same URL, same viewport, both full-page, and the
    plugin suppresses the admin bar in the preview itself.
    """
    step = "editPreviewParity"
    keys = [p.get("key") for p in MF.get("pages", []) if p.get("key")]
    if not keys:
        report["steps"][step] = {"ok": True, "entries": [], "note": "manifest declares no pages"}
        log(f"{step}: no pages — skipped")
        return
    if not args.wp_cli:
        report["steps"][step] = {"ok": None, "note": "needs --wp-cli to mint the preview nonce"}
        log(f"{step}: NOT RUN — needs --wp-cli")
        return

    # The nonce is user-bound, so it has to be minted AS the admin — a bare
    # `wp eval` runs with no user and produces one the preview will reject.
    minted = wp_cli(["eval", 'echo wp_create_nonce("clara_ve_preview");', "--user=admin"])
    nonce = (minted or "").strip().split("\n")[-1] if isinstance(minted, str) else ""
    if not re.fullmatch(r"[a-f0-9]{6,20}", nonce or ""):
        report["steps"][step] = {"ok": False, "note": f"could not mint a preview nonce: {nonce!r}"}
        log(f"{step}: FAILED — no preview nonce")
        return

    shots = OUT / "edit-preview-parity"
    shots.mkdir(parents=True, exist_ok=True)

    # BOTH captures need the same viewport. The admin page runs at whatever
    # width the smoke opened it with (1280), the visitor context at 1440, and
    # a responsive layout differs everywhere between the two — the first run
    # failed all eight pages including the 404, which contains no token at all
    # and therefore cannot differ. A gate that reds on every conversion is
    # worse than no gate: it teaches everyone to ignore it.
    #
    # Same width, and the preview borrows the admin's cookies so it is still
    # an authorised load.
    VIEW = {"width": 1440, "height": 950}
    state = admin_page.context.storage_state()
    prev_ctx = browser.new_context(viewport=VIEW, storage_state=state)
    prev = prev_ctx.new_page()

    entries, worst = [], 0.0
    for key in keys:
        row = {"key": key}
        try:
            base = url_for(key)
            a = shots / f"{key}.visitor.png"
            b = shots / f"{key}.preview.png"

            # Logged OUT, so nothing of the admin is in the frame.
            vctx = browser.new_context(viewport=VIEW)
            vpage = vctx.new_page()
            vpage.goto(base, timeout=STRUCT_MS)
            vpage.wait_for_load_state("networkidle", timeout=STRUCT_MS)
            vpage.screenshot(path=str(a), full_page=True)
            vctx.close()

            sep = "&" if "?" in base else "?"
            prev.goto(f"{base}{sep}clara_edit=1&_clara_ve={nonce}", timeout=STRUCT_MS)
            prev.wait_for_load_state("networkidle", timeout=STRUCT_MS)
            # The admin bar is a known, expected difference and not part of the
            # design, so it is removed rather than measured. The plugin means
            # to suppress it — there is a docblock about doing so before
            # _wp_admin_bar_init — but on a DIRECT preview load it is still
            # there: #wpadminbar present, html margin-top 32px, body.admin-bar.
            # Left in, it shifts every page down 32px and every page fails,
            # including a 404 that contains no token and cannot differ.
            prev.add_style_tag(content=(
                "#wpadminbar{display:none !important}"
                "html{margin-top:0 !important}"
                "html.admin-bar,body.admin-bar{margin-top:0 !important}"
            ))
            prev.screenshot(path=str(b), full_page=True)

            ratio = _diff_ratio(a, b)
            row.update({"diffPct": round(ratio * 100, 3), "ok": ratio <= PARITY_THRESHOLD})
            worst = max(worst, ratio)
            if not row["ok"]:
                row["images"] = {"visitor": str(a), "preview": str(b)}
        except Exception as exc:  # a page that cannot be compared is not a pass
            row.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        entries.append(row)
        log(f"    {key}: " + ("%.3f%%" % row["diffPct"] if "diffPct" in row else row.get("error", "?"))
            + ("" if row.get("ok") else "   <-- FAILED"))

    prev_ctx.close()
    failed = [r["key"] for r in entries if not r.get("ok")]
    report["steps"][step] = {"ok": not failed, "worstPct": round(worst * 100, 3),
                             "entries": entries, "failed": failed}
    log(f"{step}: {'PASSED' if not failed else 'FAILED — ' + ', '.join(failed)}"
        f" (worst {worst * 100:.3f}%)")


def step_forms(browser, admin_page):
    step = "forms"
    forms = MF.get("forms") or []
    if not forms:
        report["steps"][step] = {"ok": True, "entries": [], "note": "manifest declares no forms"}
        log(f"{step}: manifest declares no forms — skipped")
        return

    results = []
    for entry in forms:
        key = page_key_for_file(entry["page"])
        sel = entry.get("selector", "form")
        purpose = "list" if entry.get("purpose") == "list" else "contact"
        rec = {"page": entry["page"], "key": key, "selector": sel, "purpose": purpose}
        log(f"{step}: connecting '{sel}' on key={key} as type={purpose}")

        try:
            frame = open_editor(admin_page, key)
            set_edit_mode(admin_page, True)
            form_el = frame.locator(sel).first
            form_el.wait_for(state="visible", timeout=STRUCT_MS)
            form_el.click(timeout=STRUCT_MS)
            panel = admin_page.locator(".cve-panel")
            panel.wait_for(state="visible", timeout=STRUCT_MS)
            does = panel.locator("div.cve-field:has(span.cve-field-label:text-is('Does')) select.cve-select")
            does.wait_for(state="visible", timeout=STRUCT_MS)
            does.select_option(purpose)
            status = save_and_wait(admin_page)
            if "Saved" not in status:
                rec["ok"] = False
                rec["detail"] = dump_failure(f"{step}-{key}-connect", admin_page, frame, f"save status '{status}'")
                results.append(rec)
                continue
        except Exception as e:
            rec["ok"] = False
            rec["detail"] = dump_failure(f"{step}-{key}-connect", admin_page, extra=str(e))
            results.append(rec)
            continue

        # Verify the STORED SOURCE, not the iframe (it does not necessarily
        # repaint after connecting — SKILL.md's gotcha, taken at its word).
        try:
            src = rest_get(admin_page, f"/clara-ve/v1/source?key={key}")
            source_text = src.get("source", "") if isinstance(src, dict) else str(src)
        except Exception as e:
            rec["ok"] = False
            rec["detail"] = f"could not read stored source: {e}"
            results.append(rec)
            continue
        if f'[wp-form type="{purpose}"' not in source_text:
            rec["ok"] = False
            rec["detail"] = dump_failure(f"{step}-{key}-marker", admin_page, frame,
                                          f'stored source has no [wp-form type="{purpose}" ...] marker')
            results.append(rec)
            continue
        rec["storedSourceMarker"] = True
        log(f"{step}: stored source carries [wp-form type=\"{purpose}\" ...]")

        # Verify the PUBLIC page's contract in a fresh, unauthenticated
        # context — never the admin session, so this is what a real visitor
        # gets.
        anon_ctx = browser.new_context()
        pub = anon_ctx.new_page()
        pub.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        try:
            pub.goto(url_for(key), timeout=STRUCT_MS)
            pub_form = pub.locator(sel).first
            pub_form.wait_for(state="visible", timeout=STRUCT_MS)
            has_form_id = pub_form.locator('input[name="form_id"]').count() > 0
            has_nonce = pub_form.locator('input[name="clara_ve_nonce"]').count() > 0
            action = pub_form.get_attribute("action") or ""
            targets_submit = action.rstrip("/").endswith("/wp-json/clara-ve/v1/submit")
        except Exception as e:
            rec["ok"] = False
            rec["detail"] = dump_failure(f"{step}-{key}-public-contract", pub, extra=str(e))
            anon_ctx.close()
            results.append(rec)
            continue
        rec["publicContract"] = {"formId": has_form_id, "nonce": has_nonce, "action": action, "targetsSubmit": targets_submit}
        if not (has_form_id and has_nonce and targets_submit):
            rec["ok"] = False
            rec["detail"] = f"public form missing part of the connected contract: {rec['publicContract']}"
            anon_ctx.close()
            results.append(rec)
            continue
        log(f"{step}: public contract present — form_id={has_form_id}, nonce={has_nonce}, action={action}")

        # Anonymous submit. Fill every visible, non-hidden, non-button
        # control with a distinct test value; clear the signed time-trap
        # (default 3s, read the real value with --wp-cli when possible).
        min_seconds = 3
        if args.wp_cli:
            got = wp_cli("echo (int) get_option('clara_ve_form_min_seconds', 3);")
            if isinstance(got, str) and got.strip().isdigit():
                min_seconds = int(got.strip())
        test_values = {}
        try:
            clear_form_rate_limits()
            controls = pub_form.locator("input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]), select, textarea")
            n = controls.count()
            for i in range(n):
                c = controls.nth(i)
                name = c.get_attribute("name") or ""
                if not name or name == "cve_hp":
                    continue
                tag = c.evaluate("el => el.tagName.toLowerCase()")
                ctype = (c.get_attribute("type") or "text").lower() if tag == "input" else tag
                if ctype in ("checkbox", "radio"):
                    # A Tailwind/shadcn choice control hides the REAL input
                    # (`class="sr-only peer"`) and styles a sibling from
                    # `peer-checked:` — so the input is unclickable by design
                    # and a visitor ticks it through the wrapping <label>.
                    # check() on the input itself then waits out its whole
                    # timeout while Playwright reports the styled sibling
                    # "intercepts pointer events". Verified live on a
                    # converted RSVP form whose radio pair used exactly this
                    # idiom: the form was correctly connected, the public
                    # contract was complete, and the gate still failed.
                    # is_visible() is NOT the discriminator: `sr-only` keeps a
                    # 1px clipped box positioned off-screen, so Playwright
                    # reports it visible and then fails the click with
                    # "element is outside of the viewport". The wrapping label
                    # is what a visitor actually clicks, so prefer it whenever
                    # one exists and only fall back to forcing the input.
                    label = c.locator("xpath=ancestor::label[1]")
                    if label.count():
                        label.first.click(timeout=STRUCT_MS)
                    if not c.is_checked():
                        c.check(timeout=STRUCT_MS, force=True)
                    # What the browser SUBMITS for a ticked control is its
                    # value attribute, not the word "checked" — recording the
                    # latter meant this field could never match the stored row
                    # and the "n/m fields matched" line understated itself.
                    test_values[name] = c.get_attribute("value") or "on"
                elif ctype == "file":
                    c.set_input_files({"name": "resume.pdf", "mimeType": "application/pdf",
                                       "buffer": b"%PDF-1.4\n% CVE smoke fixture\n"})
                elif tag == "select":
                    opt = c.locator("option").first.get_attribute("value")
                    if opt is not None:
                        c.select_option(opt)
                        test_values[name] = opt
                else:
                    val = f"smoke-{RUN_ID}@example.com" if ctype == "email" else f"smoke-{name}-{RUN_ID}"
                    c.fill(val, timeout=STRUCT_MS)
                    test_values[name] = val
            pub.wait_for_timeout((min_seconds + 1) * 1000)
            submit_btn = pub_form.locator('button[type=submit], input[type=submit], button:not([type])').first
            with pub.expect_response(lambda r: r.url.rstrip("/").endswith("/wp-json/clara-ve/v1/submit"), timeout=LONG_MS) as resp_info:
                submit_btn.click(timeout=STRUCT_MS)
            resp = resp_info.value
            body = None
            try:
                body = resp.json()
            except Exception:
                pass
            clear_form_rate_limits()
        except Exception as e:
            clear_form_rate_limits()
            rec["ok"] = False
            rec["detail"] = dump_failure(f"{step}-{key}-submit", pub, extra=str(e))
            anon_ctx.close()
            results.append(rec)
            continue

        if resp.status == 403 and body and body.get("code") == "clara_ve_turnstile":
            rec["ok"] = None
            rec["detail"] = "Turnstile is enabled on this site — an automated smoke test cannot pass the challenge. Not a script bug; disable Turnstile for this form to exercise the submit path, or treat this as NOT RUN."
            anon_ctx.close()
            results.append(rec)
            continue
        if resp.status == 429:
            # The rate limiter is not what this step checks, and its 429 is
            # correct behaviour: the theme allows five submissions per ten
            # minutes per IP, the plugin one per minute, and a run that
            # submits every declared form — or a re-run inside that window —
            # meets it. Clear both limiters and send the form once more, so
            # the submit path is actually exercised instead of the throttle.
            log(f"{step}: '{key}' hit the submit rate limit — clearing it and retrying once")
            cleared = clear_form_rate_limits()
            if cleared is not None:
                try:
                    with pub.expect_response(
                            lambda r: r.url.rstrip("/").endswith("/wp-json/clara-ve/v1/submit"),
                            timeout=LONG_MS) as retry_info:
                        submit_btn.click(timeout=STRUCT_MS)
                    resp = retry_info.value
                    try:
                        body = resp.json()
                    except Exception:
                        body = None
                except Exception as e:
                    rec["ok"] = False
                    rec["detail"] = dump_failure(f"{step}-{key}-submit-retry", pub, extra=str(e))
                    anon_ctx.close()
                    results.append(rec)
                    continue
            if resp.status == 429:
                rec["ok"] = None
                rec["detail"] = ("rate-limited (429) — the site's own anti-spam guard. "
                                 + ("Cleared both limiters and retried once; still limited."
                                    if cleared is not None
                                    else "Pass --wp-cli so the limiter can be cleared and the submit exercised.")
                                 + " NOT RUN for this form.")
                log(f"{step}: '{key}' still rate-limited after clearing — NOT RUN")
                anon_ctx.close()
                results.append(rec)
                continue
        if not resp.ok:
            rec["ok"] = False
            rec["detail"] = dump_failure(f"{step}-{key}-submit-status", pub, extra=f"submit returned {resp.status}: {body}")
            anon_ctx.close()
            results.append(rec)
            continue
        rec["submitted"] = {"status": resp.status, "body": body, "fields": test_values}
        log(f"{step}: anonymous submit accepted ({resp.status}), fields={list(test_values)}")
        anon_ctx.close()

        # DB verification + cleanup (wp-cli gated).
        if args.wp_cli:
            php = (
                "$p = get_posts(array('post_type'=>'clara_ve_submission','posts_per_page'=>1,"
                "'orderby'=>'date','order'=>'DESC'));"
                "if (!$p) { echo 'NONE'; exit; }"
                "$id = $p[0]->ID; $meta = get_post_meta($id);"
                "echo wp_json_encode(array('id'=>$id,'meta'=>array_map(function($v){return $v[0];}, $meta)));"
            )
            out = wp_cli(php, timeout=30)
            if not out or out == "NONE" or (isinstance(out, dict) and "error" in out):
                rec["dbVerified"] = False
                rec["detail"] = (rec.get("detail", "") + " | " if rec.get("detail") else "") + f"no clara_ve_submission found via wp-cli: {out}"
            else:
                try:
                    row = json.loads(out)
                    matched = {k: v for k, v in test_values.items() if row["meta"].get(k) == v}
                    rec["dbVerified"] = len(matched) > 0
                    rec["dbFieldsMatched"] = matched
                    # wp_cli treats a STRING as PHP for `wp eval`; passing the
                    # shell-looking command as a string therefore never
                    # deleted anything while the log claimed cleanup. argv
                    # form invokes the real wp-cli subcommand.
                    wp_cli(["post", "delete", str(row["id"]), "--force"], timeout=20)
                    log(f"{step}: DB row {row['id']} verified ({len(matched)}/{len(test_values)} fields matched) and cleaned up")
                except Exception as e:
                    rec["dbVerified"] = False
                    rec["detail"] = f"could not parse wp-cli submission row: {e}"
        else:
            rec["dbVerified"] = "NOT RUN — pass --wp-cli to assert the DB row and its fields"

        # Disconnect: set back to "none", save, confirm the marker is gone
        # from the stored source.
        try:
            frame = open_editor(admin_page, key)
            set_edit_mode(admin_page, True)
            form_el = frame.locator(sel).first
            form_el.wait_for(state="visible", timeout=STRUCT_MS)
            form_el.click(timeout=STRUCT_MS)
            panel = admin_page.locator(".cve-panel")
            panel.wait_for(state="visible", timeout=STRUCT_MS)
            does = panel.locator("div.cve-field:has(span.cve-field-label:text-is('Does')) select.cve-select")
            does.wait_for(state="visible", timeout=STRUCT_MS)
            does.select_option("none")
            status = save_and_wait(admin_page)
            src_after = rest_get(admin_page, f"/clara-ve/v1/source?key={key}")
            source_after = src_after.get("source", "") if isinstance(src_after, dict) else str(src_after)
            restored = "[wp-form" not in source_after and "Saved" in status
            rec["disconnected"] = restored
            if not restored:
                rec["detail"] = (rec.get("detail", "") + " | " if rec.get("detail") else "") + \
                    f"disconnect did not remove the [wp-form] marker (save status '{status}')"
        except Exception as e:
            rec["disconnected"] = False
            rec["detail"] = (rec.get("detail", "") + " | " if rec.get("detail") else "") + f"disconnect step raised: {e}"

        rec["ok"] = bool(rec.get("storedSourceMarker") and rec["publicContract"]["targetsSubmit"]
                          and rec.get("submitted") and rec.get("disconnected"))
        results.append(rec)

    ok_overall = all((r["ok"] is not False) for r in results)  # None ("NOT RUN"-ish, e.g. Turnstile-blocked) does not fail the gate
    report["steps"][step] = {"ok": ok_overall, "entries": results}


# ---------------------------------------------------------------------------
# Run.
# ---------------------------------------------------------------------------

with sync_playwright() as p:
    browser = p.chromium.launch()
    if not step_preflight(browser):
        log(f"WordPress at {WP} is not reachable — nothing downstream can be tested.")
        # Same reason as at the end: an unreachable WordPress is not a pass.
        report["passed"] = False
        report["failed"] = ["preflight"]
        Path(OUT / "report.json").write_text(json.dumps(report, indent=2))
        browser.close()
        sys.exit(2)

    contract_path = find_contract_file()
    parts, menus_from_contract = parse_contract(contract_path)
    log(f"theme contract: {contract_path or 'NOT FOUND'} — {len(parts)} declared part(s), {len(menus_from_contract)} menu(s)")
    report["themeContract"] = {"path": str(contract_path) if contract_path else None,
                                "parts": parts, "menus": menus_from_contract}

    login_result = step_login(browser)
    public_ctx = browser.new_context()
    public_page = public_ctx.new_page()
    public_page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

    if login_result:
        admin_ctx, admin_page = login_result
        admin_page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        if args.only_page_roots:
            step_page_edit_roots(admin_page)
        else:
            step_text_edit_and_idempotent_save(admin_page)
            step_page_edit_roots(admin_page)
            step_media_reachable(admin_page)
            step_chrome_parts(admin_page, parts)
            step_front_menu_panel(admin_page)
            step_forms(browser, admin_page)
            step_edit_preview_parity(browser, admin_page)
        admin_ctx.close()
    elif not args.only_page_roots:
        for s in ("textEditIdempotentSave", "pageEditRoots", "mediaReachable", "chromeParts", "frontMenuPanel", "forms"):
            report["steps"][s] = {"ok": None, "note": "NOT RUN — no --admin credentials or login failed"}

    if not args.only_page_roots:
        step_menus(public_page)
        step_mobile_drawer(browser)

    public_ctx.close()
    browser.close()

failed = [k for k, v in report["steps"].items() if isinstance(v, dict) and v.get("ok") is False]
not_run = [k for k, v in report["steps"].items() if isinstance(v, dict) and v.get("ok") is None]

# The verdict goes IN the report, not only to the terminal. It used to be
# computed on the next line down, after the write — so the file said nothing
# about whether the smoke passed, and stage 6.5 read that silence as a pass.
# A failed smoke was reported to the service as green for as long as this
# existed. Anything that reads report.json must be able to see the answer.
# A step that never ran is not a step that passed. The pipeline always passes
# --admin (SKILL.md stage 5), so `not_run` here means the login broke or a
# step threw — and reporting green on that told the service the editor was
# verified when three quarters of the checks had not executed. Seen live:
# "SMOKE PASSED — 8 step(s); failed: none; not run: [6 steps]".
#
# A deliberate skip is recorded as ok=True with a note (see step_forms when
# the manifest declares no forms), so None only ever means "did not happen".
report["passed"] = not failed and not not_run
report["failed"] = failed
report["notRun"] = not_run
Path(OUT / "report.json").write_text(json.dumps(report, indent=2))

log(f"\n{'SMOKE PASSED' if report['passed'] else 'SMOKE FAILED'} — {len(report['steps'])} step(s); "
    f"failed: {failed or 'none'}; not run: {not_run or 'none'} -> {OUT / 'report.json'}")
if not_run and not failed:
    log("  (not a pass: those steps never executed, so nothing about them was verified)")
sys.exit(0 if report["passed"] else 1)
