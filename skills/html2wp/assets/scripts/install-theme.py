#!/usr/bin/env python3
"""Stage 5 install through the REAL WordPress admin UI — the owner's path,
driven by Playwright instead of by hand.

  python3 install-theme.py --wp http://<site> --theme <theme.zip> \\
      --out {workspace}/install-theme [--editor <visual-edit.zip>] \\
      [--manifest conversion-manifest.json] \\
      [--wp-cli 'docker exec <container> wp --allow-root'] \\
      [--env .test-env-<slug>.json] [--admin user:pass] [--apply-timeout 300]

`--env` reads `url` and `wpCli` from the state file `test-env.sh up` wrote;
an explicit `--wp` / `--wp-cli` wins over it.

Steps, each screenshotted into --out and scraped for admin errors:
  1. log in (wp-login.php, waits for #wpadminbar like smoke-editor.py)
  2. Themes > Add New > Upload: the theme ZIP
  3. Activate from the upload result screen
  4. CLICK the theme's own setup notice — never a direct URL. The notice is
     what an owner sees; a direct URL would pass on a theme whose notice
     never renders.
  5. Read the import PLAN, then "Apply bundled content" with
     expect_navigation() around click(no_wait_after=True), so the timeout
     that applies is --apply-timeout and not Locator.click()'s own 30 s
     navigation wait (a bare click() on a real import fails looking exactly
     like a broken selector). A navigation timeout is NOT a failure: PHP
     keeps importing, so the import state option is polled via wp-cli until
     it reads complete/failed, and only that decides.
  6. Plugins > Add New > Upload: the editor ZIP, then Activate — only when
     --editor is given (a free-tier conversion ships no editor). Local path
     only; downloading the release ZIP from GitHub (`editor.install` is a
     release PAGE, not a ZIP) is a follow-up, not done here.

Proof the apply did what it said (with --wp-cli; each proof that could not
run is printed NOT RUN, never counted as passed):
  - the plan AFTER apply is all zeros (conflicts are printed, never fatal —
    WooCommerce's own pages collide with a shop's pages legitimately);
  - `{prefix}_theme_import_state.status` is `complete`;
  - plan before − plan after == the screen's `data-html2wp-applied` (only
    when the redirect landed; the transient behind it lives one minute);
  - the state's posts / media / products counts equal the rows the bundle
    inside the ZIP carries (read from the bundle's own row files, the
    authoritative count).
wp-cli is used READ-ONLY here: `option get` and `eval` of the theme's own
read-only plan function. Every write goes through the UI.

Admin screens are scraped for `.notice-error`, `#message.error`, the
wizard's failed-import notice and PHP's own "Fatal error / Parse error /
Warning / Deprecated:" lines (a weak signal wherever display_errors is
off — the WordPress notices are the real one). Any hit fails that step.

--update is the change after delivery (apply-change.py): the same theme is
already installed and active in the preview, so the upload takes WordPress's
own "Replace active with uploaded", activation is skipped when the theme is
still the active one, and the setup screen is opened by its address (after an
update no notice shows). The apply and its proofs are the same: the importer
refreshes every page whose stored source is still the bundle's own, and an
owner's edit is never overwritten.

Exit 0 = installed (and proven, when --wp-cli is given); 1 = a step failed (report.json + the
screenshot of the failing screen say which and why); 2 = usage.
"""

import argparse, json, re, shlex, subprocess, sys, time, zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from capture_ready import fill_login  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
ap.add_argument("--wp", default="", help="WordPress base URL, e.g. http://localhost:55001")
ap.add_argument("--theme", required=True, help="the theme ZIP to upload")
ap.add_argument("--editor", default="", help="local Visual Edit plugin ZIP; omitted = no editor installed (free tier)")
ap.add_argument("--out", required=True, help="directory for report.json and per-step screenshots")
ap.add_argument("--manifest", default="", help="conversion-manifest.json (site.prefix); falls back to the ZIP")
ap.add_argument("--wp-cli", default="", dest="wp_cli",
                help="READ-ONLY wp-cli prefix, e.g. 'docker exec <ct> wp --allow-root'. Without it the state/plan proofs are NOT RUN.")
ap.add_argument("--env", default="", help=".test-env-<slug>.json written by test-env.sh up (supplies url and wpCli)")
ap.add_argument("--admin", default="admin:admin123", help="user:pass")
ap.add_argument("--apply-timeout", type=int, default=300, help="seconds to wait on the apply navigation (default 300)")
ap.add_argument("--update", action="store_true",
                help="replace the same theme already installed (a change after delivery), not a clean install")
args = ap.parse_args()


def usage(msg):
    print(f"install-theme.py: {msg}", file=sys.stderr)
    sys.exit(2)


if args.env:
    try:
        env = json.loads(Path(args.env).read_text())
    except (OSError, ValueError) as e:
        usage(f"cannot read --env {args.env}: {e}")
    args.wp = args.wp or env.get("url", "")
    args.wp_cli = args.wp_cli or env.get("wpCli", "")
if not args.wp:
    usage("--wp (or --env) is required")
WP = args.wp.rstrip("/")
THEME_ZIP = Path(args.theme)
if not THEME_ZIP.is_file():
    usage(f"theme ZIP not found: {THEME_ZIP}")
EDITOR_ZIP = Path(args.editor) if args.editor else None
if EDITOR_ZIP and not EDITOR_ZIP.is_file():
    usage(f"editor ZIP not found: {EDITOR_ZIP}")
if ":" not in args.admin:
    usage("--admin must be user:pass")
OUT = Path(args.out)
OUT.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# What the ZIP says it carries — the reference the import is measured against
# ---------------------------------------------------------------------------

def read_bundle(zip_path):
    """Theme slug, the importer's prefix and the bundle's row counts, read
    straight from the ZIP that is about to be uploaded."""
    info = {"slug": None, "prefix": None, "rows": {}, "contains": None}
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        usage(f"{zip_path} is not a ZIP: {e}")
    names = zf.namelist()
    tops = {n.split("/", 1)[0] for n in names if "/" in n}
    if len(tops) == 1:
        info["slug"] = tops.pop()

    def member(rel):
        hits = [n for n in names if n.endswith("/" + rel) and n.count("/") == rel.count("/") + 1]
        return hits[0] if hits else None

    ci = member("inc/content-import.php")
    if ci:
        m = re.search(r"'([a-z0-9_]+)_theme_import_state'", zf.read(ci).decode("utf-8", "replace"))
        if m:
            info["prefix"] = m.group(1)
    for key, rel in (("posts", "clara-content/posts.json"), ("media", "clara-content/media/index.json"),
                     ("products", "clara-content/products.json"), ("sources", "clara-content/sources/index.json")):
        name = member(rel)
        if not name:
            continue
        try:
            data = json.loads(zf.read(name))
        except ValueError:
            continue
        if isinstance(data, list):
            info["rows"][key] = len(data)
    mf = member("clara-content/manifest.json")
    if mf:
        try:
            info["contains"] = json.loads(zf.read(mf)).get("contains")
        except ValueError:
            pass
    return info


BUNDLE = read_bundle(THEME_ZIP)
PREFIX = None
if args.manifest:
    try:
        PREFIX = (json.loads(Path(args.manifest).read_text()).get("site") or {}).get("prefix") or None
    except (OSError, ValueError) as e:
        usage(f"cannot read --manifest {args.manifest}: {e}")
PREFIX = PREFIX or BUNDLE["prefix"]
if BUNDLE["prefix"] and PREFIX != BUNDLE["prefix"]:
    usage(f"manifest prefix '{PREFIX}' is not the ZIP's importer prefix '{BUNDLE['prefix']}' — wrong ZIP for this manifest?")
if not re.fullmatch(r"[a-z0-9_]+", PREFIX or ""):
    usage("could not determine the theme prefix (pass --manifest, or a ZIP carrying inc/content-import.php)")
STATE_OPTION = f"{PREFIX}_theme_import_state"


# ---------------------------------------------------------------------------
# Read-only wp-cli
# ---------------------------------------------------------------------------

def wp_cli(cli_args, timeout=60):
    """None when no --wp-cli was given; {"error": ...} on failure; stdout otherwise."""
    if not args.wp_cli:
        return None
    try:
        out = subprocess.run(shlex.split(args.wp_cli) + list(cli_args), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": "wp-cli timed out"}
    if out.returncode != 0:
        return {"error": out.stderr.strip() or f"wp-cli exited {out.returncode}"}
    return out.stdout.strip()


def wp_json(cli_args):
    out = wp_cli(cli_args)
    if out is None or isinstance(out, dict):
        return out
    try:
        return json.loads(out)
    except ValueError:
        return {"error": f"not JSON: {out[:200]}"}


def read_state():
    return wp_json(["option", "get", STATE_OPTION, "--format=json"])


def read_plan():
    # The theme's own preview function: it only looks records up, it writes
    # nothing — the same numbers the apply handler subtracts.
    return wp_json(["eval", f"echo wp_json_encode( {PREFIX}_import_plan() );"])


PLAN_COUNTS = ("pages", "posts", "media", "menus", "sources", "defaults", "products")


def plan_total(plan):
    # The screen's data-html2wp-applied sums exactly these five.
    return sum(int(plan.get(k) or 0) for k in ("pages", "posts", "media", "menus", "sources"))


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

T0 = time.monotonic()
report = {"passed": False, "wp": WP, "theme": str(THEME_ZIP), "editor": str(EDITOR_ZIP) if EDITOR_ZIP else None,
          "prefix": PREFIX, "bundle": BUNDLE, "steps": [], "proofs": {}, "failure": None}

# PHP's html_errors form is `<b>Warning</b>:  msg`, the plain form `Warning: msg`.
PHP_PROBLEM = re.compile(r"(?:<b>)?(Fatal error|Parse error|Warning|Deprecated)(?:</b>)?:\s+.{0,300}? on line <?b?>?\d+", re.S)
CRITICAL = "There has been a critical error on this website"


class StepFailed(Exception):
    pass


def log(msg):
    print(msg, flush=True)


def scrape(page):
    html = page.content()
    php = [m.group(0)[:300] for m in PHP_PROBLEM.finditer(html)]
    if CRITICAL in html:
        php.append(CRITICAL)
    errors = []
    # Visible ones only: the dashboard carries hidden `notice-error` shells
    # (Community Events' "An error occurred", "This widget requires
    # JavaScript") that WordPress only reveals when something failed.
    for sel in (".notice-error", "#message.error", "div.error"):
        for el in page.query_selector_all(sel):
            if not el.is_visible():
                continue
            text = re.sub(r"\s+", " ", el.inner_text()).strip()
            if text and text[:300] not in errors:
                errors.append(text[:300])
    message = page.query_selector("#message")
    return php, errors, (re.sub(r"\s+", " ", message.inner_text()).strip()[:300] if message else None)


def step(page, name, ok, why="", **extra):
    shot = OUT / f"{len(report['steps']):02d}-{name}.png"
    try:
        page.screenshot(path=str(shot), full_page=True)
    except Exception as e:  # a screenshot failing must not hide the step's own verdict
        shot = f"screenshot failed: {e}"
    php, errors, message = scrape(page)
    row = {"step": name, "ok": bool(ok) and not php and not errors, "s": round(time.monotonic() - T0, 1),
           "url": page.url.replace(WP, ""), "screenshot": str(shot), "phpProblems": php, "adminErrors": errors,
           "message": message, **extra}
    if not ok and why:
        row["why"] = why
    report["steps"].append(row)
    log(f"{name}: {'OK' if row['ok'] else 'FAILED'} ({row['s']}s)"
        + (f" — {why}" if not ok and why else "")
        + "".join(f"\n  php: {p}" for p in php) + "".join(f"\n  admin error: {e}" for e in errors))
    if not row["ok"]:
        raise StepFailed(name)
    return row


def login(page):
    user, _, pw = args.admin.partition(":")
    page.goto(f"{WP}/wp-login.php")
    fill_login(page, user, pw)  # read back and retried: see lib/capture_ready.py
    page.click("#wp-submit")
    try:
        page.wait_for_selector("#wpadminbar", timeout=60_000)
        ok = True
    except Exception:
        ok = False
    step(page, "login", ok, "no #wpadminbar after submitting the login form")


def upload_theme(page):
    page.goto(f"{WP}/wp-admin/theme-install.php")
    page.click(".upload-view-toggle")
    page.set_input_files("#themezip", str(THEME_ZIP))
    with page.expect_navigation(timeout=180_000):
        page.click("#install-theme-submit")
    body = page.content()
    if page.locator("a.update-from-upload-overwrite").count():
        if not args.update:
            step(page, "theme-uploaded", False,
                 "a theme with this folder name is already installed — this environment is not clean; run `test-env.sh reset <slug>` first")
        # WordPress's own "Replace active with uploaded".
        with page.expect_navigation(timeout=180_000):
            page.locator("a.update-from-upload-overwrite").first.click()
        body = page.content()
        step(page, "theme-uploaded", "updated successfully" in body.lower() or "installed successfully" in body.lower(),
             "the replace screen does not say the theme was updated")
        return
    step(page, "theme-uploaded", "installed successfully" in body.lower(),
         "the upload screen does not say the theme installed successfully")


def activate_theme(page):
    if args.update and not page.locator("a.activatelink").count():
        # Replacing the active theme leaves it active: nothing to activate.
        return step(page, "theme-activated", True, note="the replaced theme stays active")
    link = page.locator("a.activatelink").first
    if not link.count():
        step(page, "theme-activated", False, "no Activate link on the upload result screen")
    with page.expect_navigation():
        link.click()
    step(page, "theme-activated", "activated=true" in page.url or "New theme activated" in page.content(),
         "WordPress did not confirm the activation")


def open_setup_by_address(page):
    """--update: the theme's setup screen by its address — after an update
    of an imported theme no setup notice shows."""
    slug = BUNDLE.get("slug")
    if not slug:
        step(page, "setup-screen", False, "the theme ZIP names no slug to find its setup screen by")
    page.goto(f"{WP}/wp-admin/themes.php?page={slug}-setup")
    status = page.locator("[data-html2wp-import-status]").first
    if not status.count():
        step(page, "setup-screen", False, f"no setup screen at themes.php?page={slug}-setup")
    text = re.sub(r"\s+", " ", status.inner_text())
    return step(page, "setup-screen", True, importStatus=status.get_attribute("data-html2wp-import-status"),
                planText=text[:800])


def open_setup_from_notice(page):
    # The notice's own button — `.notice-warning a.button` pointing at the
    # theme's setup page (setup-wizard.php.tpl, {prefix}_setup_notice).
    notice = page.locator(".notice.notice-warning a.button[href*='themes.php?page=']").first
    if not notice.count():
        step(page, "setup-notice", False,
             "the theme's 'content setup is ready' notice is not on the screen after activation")
    step(page, "setup-notice", True)
    with page.expect_navigation():
        notice.click()
    status = page.locator("[data-html2wp-import-status]").first
    if not status.count():
        step(page, "setup-screen", False, "the notice did not lead to the theme's setup screen")
    text = re.sub(r"\s+", " ", status.inner_text())
    return step(page, "setup-screen", True, importStatus=status.get_attribute("data-html2wp-import-status"),
                planText=text[:800])


def poll_state(deadline_s):
    """After a navigation timeout: the import runs on in PHP. Wait for the
    state option to leave `running`."""
    end = time.monotonic() + deadline_s
    last = None
    while time.monotonic() < end:
        last = read_state()
        if isinstance(last, dict) and last.get("status") in ("complete", "failed"):
            return last
        time.sleep(5)
    return last


def apply_bundle(page):
    setup_url = page.url
    plan_before = read_plan()
    report["proofs"]["planBefore"] = plan_before
    apply = page.locator("a[data-html2wp-apply]").first
    if not apply.count():
        step(page, "applied", False, "no 'Apply bundled content' button (a[data-html2wp-apply])")
    started = time.monotonic()
    timed_out = False
    try:
        with page.expect_navigation(timeout=args.apply_timeout * 1000):
            apply.click(no_wait_after=True)
    except Exception as e:
        timed_out = True
        log(f"apply: navigation did not complete in {args.apply_timeout}s ({type(e).__name__}) — "
            "that is not a failed import; reading the import state instead")
    apply_s = round(time.monotonic() - started, 1)

    applied_attr = None
    import_error = None
    if timed_out:
        if not args.wp_cli:
            step(page, "applied", False,
                 "the apply navigation timed out and there is no --wp-cli to read the import state with")
        state = poll_state(max(args.apply_timeout, 300))
        report["proofs"]["stateAfterTimeout"] = state
        # The notice is gone once the import completes; the setup URL is the
        # only way back, which is fine AFTER the apply.
        page.goto(setup_url)
    else:
        applied = page.locator("[data-html2wp-applied]").first
        if applied.count():
            applied_attr = int(applied.get_attribute("data-html2wp-applied") or 0)
            if "notice-error" in (applied.get_attribute("class") or ""):
                import_error = re.sub(r"\s+", " ", applied.inner_text()).strip()[:500]
    if import_error:
        step(page, "applied", False, f"the setup screen reports the import failed: {import_error}",
             applyS=apply_s, navigationTimedOut=timed_out)
    if not timed_out and applied_attr is None:
        step(page, "applied", False, "the apply redirect landed without the result notice (data-html2wp-applied)",
             applyS=apply_s, navigationTimedOut=timed_out)
    report["proofs"]["appliedOnScreen"] = applied_attr
    return step(page, "applied", True, applyS=apply_s, navigationTimedOut=timed_out, appliedOnScreen=applied_attr)


def prove_import():
    """The four proofs. Returns a list of failure strings."""
    proofs = report["proofs"]
    failures = []
    if not args.wp_cli:
        proofs["status"] = "NOT RUN — pass --wp-cli (or --env) to prove the import from the database"
        log("proofs: NOT RUN — no --wp-cli; the import is unproven beyond what the setup screen said")
        return failures
    before = proofs.get("planBefore")
    after = read_plan()
    state = read_state()
    proofs["planAfter"], proofs["state"] = after, state
    for label, val in (("plan before apply", before), ("plan after apply", after), ("import state", state)):
        if not isinstance(val, dict) or "error" in val:
            failures.append(f"could not read the {label} via wp-cli: {(val or {}).get('error') if isinstance(val, dict) else val}")
    if failures:
        return failures

    left = {k: after[k] for k in PLAN_COUNTS if int(after.get(k) or 0)}
    if left:
        failures.append(f"the plan after apply is not empty — still outstanding: {left}")
    if after.get("conflicts"):
        proofs["conflicts"] = after["conflicts"]
        for c in after["conflicts"]:
            log(f"  conflict (reported, not fatal): {c}")

    if state.get("status") != "complete":
        failures.append(f"{STATE_OPTION}.status is {state.get('status')!r}, not 'complete'"
                        + (f" — {state['error']}" if state.get("error") else ""))
    for key in ("products_skipped", "product_failures", "products_notice"):
        if state.get(key):
            failures.append(f"import state carries {key}: {state[key]}")

    if proofs.get("appliedOnScreen") is not None:
        delta = plan_total(before) - plan_total(after)
        proofs["planDelta"] = delta
        if delta != proofs["appliedOnScreen"]:
            failures.append(f"plan before − after = {delta}, but the screen said {proofs['appliedOnScreen']} records were applied")
    else:
        proofs["planDelta"] = "NOT RUN — the apply redirect did not land, so the screen's count was never shown"

    counts = {}
    for key in ("posts", "media", "products"):
        want = BUNDLE["rows"].get(key)
        if want is None:
            continue
        got = state.get(key)
        counts[key] = {"bundle": want, "imported": got}
        if got is None or int(got) != want:
            failures.append(f"imported {key} = {got}, the bundle in the ZIP carries {want}")
    # Pages are reported, not asserted: sources/index.json also lists chrome
    # parts that never become Pages, and a page whose slug a record the theme
    # does not own already holds is left alone by design (printed above as a
    # conflict). Gate C2b checks every page's stored source.
    if "sources" in BUNDLE["rows"]:
        counts["pages"] = {"bundleSources": BUNDLE["rows"]["sources"], "imported": state.get("pages")}
    proofs["counts"] = counts
    return failures


def install_editor(page):
    sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
    from editor_install import install
    if install(page, WP, EDITOR_ZIP, step) is False:
        report["editorInstall"] = "not installed — no editor ZIP available"
        log("editor: not installed — no editor ZIP available")


def main():
    from playwright.sync_api import sync_playwright

    page = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_default_timeout(60_000)
            try:
                login(page)
                upload_theme(page)
                activate_theme(page)
                (open_setup_by_address if args.update else open_setup_from_notice)(page)
                apply_bundle(page)
                failures = prove_import()
                if failures:
                    report["proofs"]["failures"] = failures
                    for f in failures:
                        log(f"proof FAILED: {f}")
                    raise StepFailed("proofs")
                install_editor(page)
                report["passed"] = True
            except StepFailed as e:
                report["failure"] = str(e)
            except Exception as e:  # a timeout or a missing selector: keep the screen that caused it
                report["failure"] = f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"
                log(f"FAILED — {report['failure']}")
                try:
                    page.screenshot(path=str(OUT / "failure.png"), full_page=True)
                    report["failureScreenshot"] = str(OUT / "failure.png")
                except Exception:
                    pass
            finally:
                browser.close()
    except Exception as e:  # Playwright/Chromium itself would not start
        report["failure"] = report["failure"] or f"{type(e).__name__}: {e}"
        log(f"FAILED — {report['failure']}")
    finally:
        report["ms"] = int((time.monotonic() - T0) * 1000)
        (OUT / "report.json").write_text(json.dumps(report, indent=2))
    if report["passed"]:
        unproven = "" if args.wp_cli else " (import proofs NOT RUN — no --wp-cli)"
        log(f"install-theme OK in {report['ms'] / 1000:.1f}s{unproven} — report: {OUT / 'report.json'}")
        return 0
    log(f"install-theme FAILED at {report['failure']} — report: {OUT / 'report.json'}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
