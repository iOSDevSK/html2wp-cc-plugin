#!/usr/bin/env python3
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
"""The run's verdict as a file: result.json, and the deliverables beside it.

    write-result.py {workspace} [--output DIR] [--mode flash|full|astro]
                    [--status delivered|stopped] [--stopped-stage S --stopped-reason TEXT]
                    [--no-pdf]

Stage 6 (and a run that stopped cleanly) ends here. It copies what the owner
gets into the output directory — the theme ZIP, the Astro ZIP when one was
built, CONVERSION-REPORT.md — and writes result.json (schema h2wp-result/1,
docs/APP-CONTRACT.md §4), then renders conversion-report.pdf beside it with
report-pdf.py. A UI reads result.json, not the report: it is written LAST,
atomically, so its presence means the run is over.

Nothing here decides anything. Every gate row is read off the report the
gate's own script wrote (the file is named beside each row below) and a gate
with no report is `not_run` — absence is never a pass, the same rule
send-verdicts.sh keeps. A failed row stays failed; in a Flash run a failed
visual row (gate -1, A, B) is marked reportOnly: measured, not repaired.
verify-wp.py's report is two rows: B, the pages' pixels, and C, its
functional checks — a red C is never reported as a picture.

Output directory: --output, else $H2WP_OUTPUT_DIR, else {workspace}/out.
Mode: --mode, else $H2WP_MODE, else the mode progress.sh recorded
({workspace}/.h2wp-mode), else full. Status: --status, else delivered when the
theme ZIP exists, stopped when it does not.

No secret travels: no licence key, no job token, no WordPress password, no
service message (it carries paths). Exit 0; 2 = usage.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import wp_verdicts  # noqa: E402  (gate B and gate C told apart, as send-verdicts.sh does)
SCHEMA = "h2wp-result/1"
# Rows that measure fidelity to the original — a picture (gate -1, A, B) or
# the markup region by region (A2): Flash reports them and does not repair
# them. Every other row is functional, and red there is not by design.
VISUAL = {"prerender", "A", "A2", "B"}


def read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


# What the Astro project ZIP leaves out: installed packages, caches, logs and
# macOS metadata. The desktop app packaged it the same way (files.rs
# zip_astro_project).
ASTRO_SKIPPED = {"node_modules", ".astro", ".cache", ".vite", ".turbo", ".npm", ".git", ".DS_Store", ".html2wp"}


def zip_astro_project(project, dest, top):
    """The generated Astro 5 project as a ZIP under one `{top}/` folder —
    sources, public files, package metadata, config and the built dist/ — with
    the converter's astro-report.json at .html2wp/astro-report.json, so the ZIP
    can come back as a ready Astro input (detect-project.py: html2wp-astro).
    False, and nothing written, when there is no project."""
    import zipfile
    if not (project / "package.json").is_file():
        return False
    partial = dest.with_suffix(".part")
    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(project.rglob("*")):
                rel = path.relative_to(project)
                if any(p in ASTRO_SKIPPED or p.startswith("._") or p.endswith(".log") for p in rel.parts):
                    continue
                if path.is_symlink() or not path.is_file():
                    continue
                zf.write(path, f"{top}/{rel.as_posix()}")
            report = project.parent / "astro-report.json"
            if report.is_file() and not report.is_symlink():
                zf.write(report, f"{top}/.html2wp/astro-report.json")
        partial.replace(dest)
    except OSError:
        partial.unlink(missing_ok=True)
        raise
    return True


def plugin_version():
    for candidate in (HERE.parent.parent / "VERSION", HERE.parent.parent.parent.parent / "VERSION"):
        try:
            return candidate.read_text().strip()
        except OSError:
            continue
    return None


def run_mode(ws, given):
    mode = given or os.environ.get("H2WP_MODE", "")
    if not mode:
        try:
            mode = (ws / ".h2wp-mode").read_text().strip()
        except OSError:
            mode = ""
    return mode if mode in ("flash", "astro") else "full"


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def row(gid, stage, status, detail, report=None):
    return {"id": gid, "stage": stage, "status": status, "reportOnly": False, "detail": detail, "report": report}


def page_widths(report):
    """(red, measured, exempt, worst %, red pages) of a pixel gate's `pages`
    — {file: {width: {diffRatio, ok}}}, verify-static.py and verify-wp.py
    alike; an ok None cell is an exemption the gate made, not a measurement."""
    return wp_verdicts.pixel_cells(report)


def gate_prerender(ws, mode="full"):
    """Gate -1: prerender-report.json `passed` (prerender-spa.py). With
    --no-verify the capture ran and the gate did not: its warning says so.
    Flash and the Astro run skip it by design (SKILL.md), so there the row is
    not_run with byDesign — never a pass, and not a check the run forgot."""
    kind = (read(ws / "detect.json") or {}).get("kind")
    static_site = read(ws / "static-site-report.json")
    report = read(ws / "prerender-report.json")
    if report is None:
        if isinstance(static_site, dict) and static_site.get("ok") is True:
            return row("prerender", "-1", "skipped", "a static-site build: its own pages were taken without a browser "
                       "(static-site.py)", "static-site-report.json")
        if kind in ("static-html", "html2wp-astro"):
            return row("prerender", "-1", "skipped", f"the input is {kind}: nothing to prerender", "detect.json")
        return row("prerender", "-1", "not_run", "no prerender report")
    if any("parity gate skipped" in str(w) for w in report.get("warnings") or []):
        out = row("prerender", "-1", "not_run", f"{len(report.get('routes') or [])} route(s) captured; "
                  "gate -1 (running app vs capture) was not run", "prerender-report.json")
        if mode in ("flash", "astro"):
            out["byDesign"] = True
        return out
    passed = report.get("passed") is True
    return row("prerender", "-1", "passed" if passed else "failed",
               f"{len(report.get('routes') or [])} route(s); " + ("the capture matches the running app" if passed
                                                                  else "the capture differs from the running app"),
               "prerender-report.json")


def gate_pixels(ws, gid, stage, rel):
    """Gate A (verify-static/report.json):
    `passed`, and the page×width cells with ok false. scope partial is a
    fix-cycle run on some pages, never the verdict (send-verdicts.sh gate())."""
    report = read(ws / rel)
    if report is None or report.get("scope") == "partial":
        return row(gid, stage, "not_run", "no full report" if report is None else "only a partial run exists", None)
    failing, total, exempt, worst, pages = page_widths(report)
    passed = report.get("passed") is True
    also = f"; {exempt} exempt by the gate" if exempt else ""
    if passed:
        detail = f"{total} page width(s) within the threshold{also}"
    elif failing:
        detail = f"{failing} of {total} measured page width(s) over the threshold (worst {worst:.1f}%): {', '.join(pages[:8])}{also}"
    else:
        detail = "failed on a check other than the pixel comparison; read the report"
    out = row(gid, stage, "passed" if passed else "failed", detail, rel)
    if pages:
        out["pages"] = pages
    return out


def gates_b_c(ws):
    """verify-wp.py writes gate B (the pages' pixels) and gate C (routing,
    menus wired, blog fidelity, collections, stored sources, SEO) into one
    report with one `passed`. Flash reports B and must not hide C behind it,
    so they are two rows: B from the page cells, C from the checks — and a
    red report neither explains is C, never a picture."""
    rel = "verify-wp/report.json"
    report = read(ws / rel)
    if report is None or report.get("scope") == "partial":
        why = "no full report" if report is None else "only a partial run exists"
        return [row("B", "5", "not_run", why, None), row("C", "5", "not_run", why, None)]
    failing, total, exempt, worst, _ = page_widths(report)
    parts = wp_verdicts.split(report)
    pixel_pages, functional, unexplained = parts["pixelPages"], parts["functional"], parts["unexplained"]
    also = f"; {exempt} exempt by the gate" if exempt else ""
    b = row("B", "5", "failed" if pixel_pages else "passed",
            f"{failing} of {total} measured page width(s) over the threshold (worst {worst:.1f}%): {', '.join(pixel_pages[:8])}{also}"
            if pixel_pages else f"{total} measured page width(s) within the threshold{also}", rel)
    if pixel_pages:
        b["pages"] = pixel_pages
    c_failed = not parts["c"]
    c = row("C", "5", "failed" if c_failed else "passed",
            ("failed: " + ", ".join(functional[:8])) if functional else
            "failed on a check this summary cannot name; read the report" if unexplained else
            "routing, menus, blog, collections and stored sources as the report checked them", rel)
    return [b, c]


def gate_parity(ws):
    """Gate A2: parity-report.json (verify-parity.mjs --out default), or
    verify-parity/report.json — the two paths send-verdicts.sh reads."""
    for rel in ("parity-report.json", "verify-parity/report.json"):
        report = read(ws / rel)
        if report is not None:
            # `pages` holds only the pages with a finding; `coverage` counts
            # the pages each region was compared on.
            passed = report.get("passed") is True
            compared = max([c.get("compared", 0) for c in (report.get("coverage") or {}).values()
                            if isinstance(c, dict)] or [0])
            differing = sorted(report.get("pages") or {})
            return row("A2", "2", "passed" if passed else "failed",
                       f"{compared} page(s) compared region by region"
                       + ("" if passed else f"; markup differs on {', '.join(differing[:8]) or 'a region'}"), rel)
    return row("A2", "2", "not_run", "no parity report")


def gate_listings(ws, manifest, mode):
    """The listing pre-flight: preflight-listings.json `passed`
    (preflight-listings.mjs — run after stage 1 in Flash, and by
    convert-remote.sh before the upload in both modes)."""
    stage = "1" if mode == "flash" else "3"
    wants = any((manifest.get(k) or {}).get("present") for k in ("blog", "shop"))
    report = read(ws / "preflight-listings.json")
    if report is None:
        return row("listings", stage, "not_run" if wants else "skipped",
                   "no pre-flight report" if wants else "no blog and no shop to wire")
    passed = report.get("passed") is True
    rows = [r for r in report.get("rows") or [] if isinstance(r, dict)]
    return row("listings", stage, "passed" if passed else "failed",
               "every blog/shop selector holds its cards" if passed
               else "; ".join(str(r.get("field") or r.get("id")) for r in rows[:5]) or "refused; read the report",
               "preflight-listings.json")


def gate_convert(ws):
    """The service conversion: .h2wp-result.json (convert-remote.sh's
    outcome record) — status SUCCESS, else its code and stage. Its message
    names local paths, so it stays in the workspace."""
    result = read(ws / ".h2wp-result.json")
    if result is None:
        return row("convert", "3", "not_run", "no service conversion recorded")
    if result.get("status") == "SUCCESS":
        return row("convert", "3", "passed", "the service built the theme", ".h2wp-result.json")
    return row("convert", "3", "failed", f"{result.get('code') or 'FAILED'} at {result.get('stage') or 'an unknown stage'}",
               ".h2wp-result.json")


def gate_simple(ws, gid, stage, rel, ok_text, fail_field):
    """install-theme/report.json (`passed`, `failure`), quick-check.json
    (`passed`, `problems`), smoke-editor/report.json (`passed`, `failed`),
    woo-coverage/report.json (`passed`, `failures`)."""
    report = read(ws / rel)
    if report is None:
        return row(gid, stage, "not_run", "no report")
    passed = report.get("passed") is True
    if passed:
        return row(gid, stage, "passed", ok_text, rel)
    why = report.get(fail_field)
    if isinstance(why, list):
        why = ", ".join(str(w)[:120] for w in why[:6]) or None
    return row(gid, stage, "failed", str(why or "failed; read the report")[:600], rel)


def gates_of(ws, manifest, target, mode="full"):
    rows = [gate_prerender(ws, mode),
            gate_pixels(ws, "A", "2", "verify-static/report.json"),
            gate_parity(ws)]
    if target == "astro":
        # The Astro 5 project only: no service, no WordPress, nothing past stage 2.
        return rows
    if target == "html":
        rows.append(gate_listings(ws, manifest, mode))
    rows += [gate_convert(ws),
             gate_simple(ws, "install", "5", "install-theme/report.json",
                         "installed through WordPress and its content imported", "failure"),
             gate_simple(ws, "quick", "5", "quick-check.json",
                         "theme active, every imported route answers without a PHP error", "problems")]
    if target == "gutenberg":
        report = read(ws / "gutenberg-verification.json")
        rows.append(row("G", "5", "not_run" if report is None else "passed" if report.get("passed") is True else "failed",
                        "no Gutenberg verification report" if report is None else
                        f"scope {report.get('scope', 'full')}", "gutenberg-verification.json" if report else None))
    else:
        rows += gates_b_c(ws) + [
                 gate_simple(ws, "smoke", "5", "smoke-editor/report.json",
                             "Visual Edit Lite edits, menus and forms work", "failed")]
    if (manifest.get("shop") or {}).get("present"):
        rows.append(gate_simple(ws, "woo", "5.6", "woo-coverage/report.json", "WooCommerce covers the shop", "failures"))
    else:
        rows.append(row("woo", "5.6", "skipped", "no shop"))
    return rows


def wired_of(manifest):
    nav = [n for n in manifest.get("nav") or [] if isinstance(n, dict)]
    blog, shop = manifest.get("blog") or {}, manifest.get("shop") or {}
    return {
        # zoneSelector is written back by convert-remote.sh for a menu the
        # theme wired; `unwired` marks one that kept its static links.
        "menus": len(nav),
        "menusWired": sum(1 for n in nav if n.get("zoneSelector") and "unwired" not in n),
        "blog": {"present": bool(blog.get("present")), "posts": len(blog.get("articles") or [])},
        "shop": {"present": bool(shop.get("present")), "products": len(shop.get("products") or [])},
        "forms": len(manifest.get("forms") or []),
        "collections": len(manifest.get("collections") or []),
    }


def verdict_of(mode, gates, status="delivered"):
    """The fixed words a UI shows as they are. A check that did not run is
    never a pass: a run with one (other than gate -1, which Flash skips by
    design) says so, and only a run whose every check ran and passed says
    all checks passed."""
    prefix = {"flash": "Flash: ", "astro": "Astro: "}.get(mode, "")
    if status == "stopped":
        # No deliverable: nothing here is verified, whatever did pass.
        return prefix + "stopped"
    failed = [g for g in gates if g["status"] == "failed"]
    if any(not g.get("reportOnly") for g in failed):
        return prefix + "failed checks"
    if any(g["status"] == "not_run" and not g.get("byDesign") for g in gates):
        return prefix + "checks not run"
    if failed:
        return prefix + "not visually repaired"
    return prefix + "all checks passed" if prefix else "verified"


def write_atomic(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("workspace")
    ap.add_argument("--output", default="")
    ap.add_argument("--mode", choices=("flash", "full", "astro"), default="")
    ap.add_argument("--status", choices=("delivered", "stopped"), default="")
    ap.add_argument("--stopped-stage", default="")
    ap.add_argument("--stopped-reason", default="")
    ap.add_argument("--no-pdf", action="store_true")
    args = ap.parse_args(argv)

    ws = Path(args.workspace).resolve()
    manifest = read(ws / "conversion-manifest.json")
    if not isinstance(manifest, dict):
        # A run that stopped before stage 0 wrote one (no pages, a refused
        # input, Flash asked of a target that has none) still ends with a
        # result: stopped, and why.
        if args.status != "stopped":
            print(f"write-result: no conversion-manifest.json in {ws} (a run that stopped earlier: --status stopped)",
                  file=sys.stderr)
            return 2
        manifest = {"schema": "html2wp/2" if os.environ.get("H2WP_TARGET") == "gutenberg" else "html2wp/1"}
    out = Path(args.output or os.environ.get("H2WP_OUTPUT_DIR") or ws / "out").resolve()
    out.mkdir(parents=True, exist_ok=True)
    mode = run_mode(ws, args.mode)
    v2 = manifest.get("schema") == "html2wp/2" or manifest.get("target") == "gutenberg"
    target = "astro" if mode == "astro" else "gutenberg" if v2 else "html"
    site = manifest.get("site") or {}
    slug, version = site.get("slug") or "", site.get("version") or "1.0.0"

    files = {}
    # The intermediate Astro 5 project goes to the owner too: both targets
    # share stages 1-2.7, and the static site or its source is worth having.
    astro_zip = ws / f"{slug}-astro-{version}.zip"
    if slug and not astro_zip.is_file() and (ws / "astro-project").is_dir():
        zip_astro_project(ws / "astro-project", astro_zip, f"{slug}-astro")
    for key, name in (("theme", f"{slug}-{version}.zip"), ("astro", f"{slug}-astro-{version}.zip")):
        src = ws / name
        if slug and src.is_file():
            shutil.copyfile(src, out / name)
            files[key] = {"file": name, "sha256": digest(out / name), "bytes": (out / name).stat().st_size}
    report_md = ws / "CONVERSION-REPORT.md"
    if report_md.is_file():
        shutil.copyfile(report_md, out / "CONVERSION-REPORT.md")
    else:
        (out / "CONVERSION-REPORT.md").write_text(
            "# Conversion report\n\nThe conversion report was not written for this run. "
            "The checks it ran are in result.json and in the PDF beside it.\n")

    # The Astro run delivers the Astro project; every other run the theme.
    status = args.status or ("delivered" if ("astro" if target == "astro" else "theme") in files else "stopped")
    gates = gates_of(ws, manifest, target, mode)
    for g in gates:
        g["reportOnly"] = mode in ("flash", "astro") and g["status"] == "failed" and g["id"] in VISUAL
    env = read(ws / f".test-env-{slug}.json") if slug else None
    job = read(ws / ".h2wp-result.json") or {}
    # send-verdicts.sh leaves .h2wp-verdicts-sent only when the service took
    # the verdicts (stage 6.5 runs before this in Flash; a Full run may call
    # this again after it).
    sent = (ws / ".h2wp-verdicts-sent").exists()
    doc = {
        "schema": SCHEMA,
        "plugin": plugin_version(),
        "mode": mode,
        "target": target,
        "status": status,
        "stopped": ({"stage": args.stopped_stage or None, "reason": args.stopped_reason or "no theme was built"}
                    if status == "stopped" else None),
        "site": {"name": site.get("name"), "slug": slug or None, "version": version},
        "theme": files.get("theme"),
        "astro": files.get("astro"),
        # The built static site, for a UI's site preview or a static deploy.
        # path is resolved (the host's own path under a same-path mount);
        # workspacePath is the same folder relative to {workspace}.
        "builtSite": ({"path": str(ws / "astro-project" / "dist"), "workspacePath": "astro-project/dist",
                       "index": "index.html"}
                      if (ws / "astro-project" / "dist" / "index.html").is_file() else None),
        "report": {"markdown": "CONVERSION-REPORT.md", "pdf": None},
        "preview": ({"url": env.get("url"), "user": "admin", "stateFile": str(ws / f".test-env-{slug}.json")}
                    if isinstance(env, dict) and env.get("url") else None),
        "verdict": verdict_of(mode, gates, status),
        "gates": gates,
        "wired": wired_of(manifest),
        "service": {"edition": job.get("edition"), "jobId": job.get("jobId"),
                    "verdictsSent": sent},
    }

    # The PDF reads the result, so the workspace copy comes first; the output
    # copy — the one a UI waits for — is the last file this script writes.
    write_atomic(ws / "result.json", doc)
    if not args.no_pdf:
        rendered = subprocess.run([sys.executable, str(HERE / "report-pdf.py"), "--result", str(ws / "result.json"),
                                   "--report", str(out / "CONVERSION-REPORT.md"), "--out", str(out / "conversion-report.pdf")],
                                  capture_output=True, text=True, timeout=300)
        if rendered.returncode == 0 and (out / "conversion-report.pdf").is_file():
            doc["report"]["pdf"] = "conversion-report.pdf"
        else:
            doc["report"]["pdfError"] = (rendered.stderr.strip().splitlines() or ["report-pdf.py failed"])[-1][:300]
        write_atomic(ws / "result.json", doc)
    write_atomic(out / "result.json", doc)
    print(out / "result.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
