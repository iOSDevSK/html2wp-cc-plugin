#!/usr/bin/env python3
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
"""Flash stage 5.6: the cart probe's red rows, repaired by their levers.

    woo-repair.py {workspace} [--wp <url>]

After audit-woo-coverage.py, when its cart probe found what a shopper
watches broken — no cart count in the header, a count that does not follow
the cart, two sizes merged into one line — this spends the repair budget on
exactly those rows, each through its one scripted lever
(assets/repair-levers.json) and nothing free-form:

  progress.sh repair 5.6 <lever> <signature>        counted; refused past the budget
  woo-shims.py <the lever's patch>                  deterministic theme edit
  apply-change.py --repair 5.6                      into the run's ZIP and the preview
  audit-woo-coverage.py --probe-cart                the probe again
  progress.sh repaired 5.6 fixed|failed             closed

A row whose lever was already spent (the cart count's one lever covers both
of its rows), or that the budget refuses, stays red: it is in the woo gate's
failures, with no second attempt spent on it. The stage itself is still yours to close (progress.sh done
or warn).

Exit 0 = the probe's rows are green (or there were none); 1 = some stay red;
2 = usage.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ORDER = ("cart-count-missing", "cart-count-stale", "cart-options-merged")


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def failures(ws):
    return [k for k in ORDER if k in ((read(ws / "woo-coverage" / "report.json") or {}).get("failures") or [])]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("workspace")
    ap.add_argument("--wp", default="", help="the preview's address (default: the workspace's test-env state)")
    args = ap.parse_args(argv)
    ws = Path(args.workspace).resolve()
    manifest = read(ws / "conversion-manifest.json")
    if not isinstance(manifest, dict):
        print(f"woo-repair: no conversion-manifest.json in {ws}", file=sys.stderr)
        return 2
    slug = (manifest.get("site") or {}).get("slug") or ""
    url = args.wp or ((read(ws / f".test-env-{slug}.json") or {}).get("url") or "")
    if not url:
        print(f"woo-repair: WordPress is not up for '{slug}' (no .test-env-{slug}.json) — test-env.sh up {slug}",
              file=sys.stderr)
        return 2
    table = read(HERE.parent / "repair-levers.json") or {}
    env = {**os.environ, "H2WP_WORKSPACE": str(ws)}
    progress = lambda *a: subprocess.run(["bash", str(HERE / "progress.sh"), *a], env=env, capture_output=True, text=True)
    spent, given_up = set(), set()
    initial = read(ws / "woo-coverage" / "report.json")
    if not isinstance(initial, dict) or not isinstance(initial.get("failures"), list):
        print("woo-repair: no valid cart probe report; run the audit before repair", file=sys.stderr)
        return 2
    red = failures(ws)
    if not red:
        print("woo-repair: the cart probe is green — nothing to repair")
        return 0
    while red:
        signature = red[0]
        lever = next((lv for lv in (table.get("signatures", {}).get(signature) or {}).get("levers", [])
                      if lv not in spent), None)
        if lever is None:
            print(f"woo-repair: {signature} — its lever was already applied and it is still red; recorded red")
            given_up.add(signature)
        else:
            spec = table["levers"][lever]
            opened = progress("repair", "5.6", lever, signature)
            if opened.returncode != 0:
                print(f"woo-repair: {signature} — the budget refused another attempt:{opened.stderr.rstrip()}")
                break
            print(opened.stdout.rstrip())
            spent.add(lever)
            script, *flags = spec["script"].split()
            patched = subprocess.run([sys.executable, str(HERE / script), str(ws), *flags],
                                     capture_output=True, text=True)
            applied = patched.returncode == 0 and subprocess.run(
                [sys.executable, str(HERE / "apply-change.py"), str(ws), "--repair", "5.6", "--what", spec["label"]],
                env=env, capture_output=True, text=True).returncode in (0, 3)
            report_path = ws / "woo-coverage" / "report.json"
            previous = report_path.stat().st_mtime_ns if report_path.exists() else 0
            command = [sys.executable, str(HERE / "audit-woo-coverage.py"), "--wp", url, "--workspace", str(ws), "--probe-cart"]
            if (read(ws / "progress.json") or {}).get("mode") == "full":
                command = [sys.executable, str(HERE / "repair-check.py"), str(ws), "5.6", "--", *command]
            try:
                checked = subprocess.run(command, env=env, capture_output=True, text=True, timeout=910)
                current = read(report_path)
                fresh = report_path.exists() and report_path.stat().st_mtime_ns > previous
                fixed = (applied and checked.returncode == 0 and fresh and isinstance(current, dict)
                         and isinstance(current.get("failures"), list) and signature not in failures(ws))
            except (OSError, subprocess.TimeoutExpired):
                progress("repaired", "5.6", "failed", "cart probe did not complete; previous report is not proof")
                return 1
            progress("repaired", "5.6", "fixed" if fixed else "failed",
                     f"{spec['label']}: " + ("the probe is green for it" if fixed else
                                             ("the patch did not apply" if not applied else "the probe is still red")))
            print(f"woo-repair: {signature} — {spec['label']} — {'FIXED' if fixed else 'still red'}")
            if not fixed:
                given_up.add(signature)
        red = [s for s in failures(ws) if s not in given_up]
    left = failures(ws)
    print("woo-repair: " + ("the cart probe is green" if not left else f"still red: {', '.join(left)}"))
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main())
