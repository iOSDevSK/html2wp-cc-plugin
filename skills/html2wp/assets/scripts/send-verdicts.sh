#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
# Tell the service what the gates said.
#
#   send-verdicts.sh <workspace> [--outcome=delivered|abandoned|in-progress] [--api=URL] [--dry-run]
#
# The service converts a site and never learns whether the result was CORRECT:
# gates A, A2, B, C, the editor smoke test and the Woo audit all run here, on
# your machine, against a WordPress it never sees. Every conversion it records
# therefore says "the transform succeeded", which is a statement about the
# service and not about the theme.
#
# This closes that loop, and it is REQUIRED — not as a checkbox, but because
# the next conversion asks for it. Run it at stage 6, when the gates have run.
#
# WHAT IT SENDS: gate names, verdicts, page counts, the worst percentage, and
# the page KEYS that failed — the short names you chose (`about`, `shop`).
# An HTML-theme job (manifest html2wp/1) reports A, A2, B, C, smoke-editor and
# woo-coverage; a native Gutenberg job (html2wp/2) reports A, A2, G-front,
# G-editor, G-roundtrip, G-import and woo-coverage, read off
# gutenberg-verification.json (the service's docs/GUTENBERG-VERDICTS.md).
#
# WHAT IT DOES NOT SEND, and the server drops if a future client ever tries:
# no URL, no markup, no copy, no screenshots, no file paths, no licence key,
# no site name. Read `verdicts.ts` on the service for the whitelist itself.
#
# --dry-run prints the payload instead of sending it. Nothing about a
# conversion changes either way; a failed send is a warning, never a stop.
set -euo pipefail

WS=""; OUTCOME=""; API="${H2WP_API:-https://api.html2wp.dev}"; DRY=0
for arg in "$@"; do
  case "$arg" in
    --outcome=*) OUTCOME="${arg#--outcome=}" ;;
    --api=*)     API="${arg#--api=}" ;;
    --dry-run)   DRY=1 ;;
    --*) echo "unknown flag: $arg" >&2; exit 2 ;;
    *) WS="$arg" ;;
  esac
done
[ -n "$WS" ] || { echo "usage: send-verdicts.sh <workspace> [--outcome=delivered] [--dry-run]" >&2; exit 2; }
WS="$(cd "$WS" && pwd)"; API="${API%/}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIENT_VERSION="${H2WP_CLIENT_VERSION:-}"
for VERSION_FILE in "$SCRIPT_DIR/../../VERSION" "$SCRIPT_DIR/../../../../VERSION"; do
  [ -n "$CLIENT_VERSION" ] && break
  [ ! -f "$VERSION_FILE" ] || CLIENT_VERSION="$(tr -d '[:space:]' < "$VERSION_FILE")"
done

CLIENT_HOST="${H2WP_HOST:-}"
case "$CLIENT_HOST" in
  codex|claude-code) ;;
  '')
    if [ -n "${CODEX_SESSION_ID:-}${CODEX_THREAD_ID:-}${CODEX_CI:-}" ]; then
      CLIENT_HOST=codex
    elif [ -n "${CLAUDECODE:-}${CLAUDE_CODE_ENTRYPOINT:-}" ]; then
      CLIENT_HOST=claude-code
    else
      CLIENT_HOST=claude-code
    fi
    ;;
  *) echo "refusing: H2WP_HOST must be codex or claude-code" >&2; exit 2 ;;
esac

# Where convert-remote.sh saved the job: H2WP_JOB_STATE moves it, as there.
JOB_FILE="${H2WP_JOB_STATE:-$WS/.h2wp-job.json}"
[ -f "$JOB_FILE" ] || { echo "no $JOB_FILE — this workspace has not been converted by the service" >&2; exit 1; }
TOKEN="$(python3 - "$JOB_FILE" <<'PY'
import json, sys
value = json.load(open(sys.argv[1])).get("token", "")
if not isinstance(value, str) or not value:
    raise SystemExit(1)
print(value)
PY
)" || { echo "$JOB_FILE has no job token — start a fresh conversion before reporting verdicts" >&2; exit 1; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

python3 - "$WS" "$JOB_FILE" "$OUTCOME" > "$TMP/payload.json" <<'PY'
import json, os, re, sys

ws, job_file, outcome = sys.argv[1:4]
job = json.load(open(job_file)).get("job", "")

def read(*parts):
    try:
        return json.load(open(os.path.join(ws, *parts)))
    except Exception:
        return None

# The service's PAGE_KEY length (transform.ts): a longer key used to drop out silently.
KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,95}$", re.I)

def keys_of(report, *fields):
    """Page keys only. A path, a URL or a sentence is not a key and is dropped."""
    out = []
    for f in fields:
        v = (report or {}).get(f)
        if isinstance(v, dict):
            v = list(v.keys())
        if isinstance(v, list):
            out += [k for k in v if isinstance(k, str) and KEY.match(k)]
    return sorted(set(out))[:40]

def steps_verdict(report):
    """Some reports carry the answer per step rather than at the top.

    Absence is not a pass. `passed is None` used to fall through to `not
    failing`, and for a report shaped {steps: {name: {ok: bool}}} that is
    unconditionally True — so a smoke run that failed three steps was sent to
    the service as green, which is the one thing stage 6.5 exists to prevent.
    Look inside before concluding anything.
    """
    steps = report.get("steps")
    if not isinstance(steps, dict):
        return None, []
    failed = [k for k, v in steps.items() if isinstance(v, dict) and v.get("ok") is False]
    # ok=None is a step that never executed — a login that broke, a step that
    # threw. It is not a pass, and treating it as one is how a smoke with six
    # of eight steps unrun reported green to the service.
    not_run = [k for k, v in steps.items() if isinstance(v, dict) and v.get("ok") is None]
    ran = [v for v in steps.values() if isinstance(v, dict) and "ok" in v]
    if not ran:
        return None, []
    return (not failed and not not_run), sorted(set(failed + not_run))


def gate(name, report, *, pages_field="pages", worst_field="worstPct", fail_fields=("failures", "failed")):
    if report is None:
        return {"gate": name, "verdict": "not-run"}
    # A gate run on part of the site (--pages: a fix cycle, a bisect) is a
    # diagnosis, not a verdict — its green says nothing about the pages it
    # skipped, and its red is about the subset only. The gate did not run on
    # the conversion, so that is what the service hears.
    if report.get("scope") == "partial":
        return {"gate": name, "verdict": "not-run"}
    passed = report.get("passed")
    failing = keys_of(report, *fail_fields)
    if passed is None:
        derived, derived_failing = steps_verdict(report)
        if derived is not None:
            passed = derived
            failing = failing or sorted(set(k for k in derived_failing if KEY.match(k)))[:40]
        else:
            passed = not failing
    entry = {"gate": name, "verdict": "passed" if passed else "failed"}
    for src, dst in ((pages_field, "pages"), (worst_field, "worstPct")):
        v = report.get(src)
        if isinstance(v, (int, float)):
            entry[dst] = v
    if failing:
        entry["failedKeys"] = failing
    not_run = keys_of(report, "notRun", "not_run")
    if not_run:
        entry["notRun"] = not_run
    return entry

def entry(name, verdict, *, pages=None, worst=None, failed=(), not_run=()):
    out = {"gate": name, "verdict": verdict}
    if isinstance(pages, int):
        out["pages"] = pages
    if isinstance(worst, (int, float)) and not isinstance(worst, bool):
        out["worstPct"] = round(min(max(worst, 0), 100), 4)
    failed = sorted(set(k for k in failed if isinstance(k, str) and KEY.match(k)))[:40]
    not_run = sorted(set(k for k in not_run if isinstance(k, str) and KEY.match(k)))[:40]
    if failed:
        out["failedKeys"] = failed
    if not_run:
        out["notRun"] = not_run
    return out


def gutenberg_gates(manifest):
    """G-front, G-editor, G-roundtrip, G-import from h2wp-local-verification/2.

    The contract is the server's docs/GUTENBERG-VERDICTS.md. Absence is not
    a pass: an empty section is not-run. Only manifest page KEYS travel —
    a route or a WordPress slug that maps to no key is left out, never sent.
    """
    names = ("G-front", "G-editor", "G-roundtrip", "G-import")
    r = read("gutenberg-verification.json")
    if not isinstance(r, dict) or r.get("schema") != "h2wp-local-verification/2":
        return [entry(n, "not-run", not_run=["no-report"]) for n in names]
    # A smoke run (--scope smoke: 1440 only, no save/reload gates) is a
    # diagnosis, like gate A's --pages: it never answers for the conversion.
    # A report from before the field existed was a full run.
    if r.get("scope", "full") != "full":
        return [entry(n, "not-run", not_run=["scope-" + str(r.get("scope"))]) for n in names]

    pages = [p for p in manifest.get("pages", []) if isinstance(p, dict) and p.get("kind") != "fragment"]
    keys = {p.get("key") for p in pages if isinstance(p.get("key"), str)}
    by_slug = {p["slug"]: p["key"] for p in pages if isinstance(p.get("slug"), str) and p.get("key") in keys}
    def norm(path):
        path = "/" + str(path or "").strip("/")
        for tail in ("/index.html", ".html"):
            if path.endswith(tail):
                path = path[: -len(tail)] or "/"
        return path.rstrip("/") or "/"
    by_source = {norm(p.get("file")): p["key"] for p in pages if p.get("key") in keys}
    by_target = {}
    routes = read("gutenberg-routes.json")
    for route in routes if isinstance(routes, list) else []:
        if isinstance(route, dict) and norm(route.get("source")) in by_source:
            by_target[norm(route.get("target"))] = by_source[norm(route.get("source"))]
    front = next((p["key"] for p in pages if p.get("kind") == "front" and p.get("key") in keys), None)
    if front:
        by_target.setdefault("/", front)
    def key_of_path(path):
        return by_target.get(norm(path))
    def key_of_slug(item):
        slug = item.get("slug")
        if slug in keys:
            return slug
        return by_slug.get(slug) or key_of_path(item.get("path"))

    aborted = ["aborted"] if r.get("error") else []
    lst = lambda v: v if isinstance(v, list) else []
    visual = [v for v in lst(r.get("visual")) if isinstance(v, dict)]
    editor = [v for v in lst(r.get("editor")) if isinstance(v, dict)]
    editor_visual = [v for v in lst(r.get("editorVisual")) if isinstance(v, dict)]
    diffs = lambda rows: [v["diff"] * 100 for v in rows
                          if isinstance(v.get("diff"), (int, float)) and not isinstance(v.get("diff"), bool)]
    out = []

    # The same pages at normal motion (R.motion[], 1440): a reveal that never
    # runs in WordPress fails the frontend gate like any other frontend row.
    motion = [v for v in lst(r.get("motion")) if isinstance(v, dict)]
    if not visual:
        out.append(entry("G-front", "not-run", not_run=aborted))
    else:
        rows = visual + motion
        ok = all(v.get("passed") is True for v in rows)
        out.append(entry("G-front", "passed" if ok else "failed",
                         pages=len({norm(v.get("path")) for v in visual}),
                         worst=max(diffs(rows), default=None),
                         failed=[key_of_path(v.get("path")) for v in rows if v.get("passed") is not True]))

    if not editor and not editor_visual:
        out.append(entry("G-editor", "not-run", not_run=aborted))
    else:
        broken = [e for e in editor if e.get("invalid") or e.get("unknown") or e.get("unresolvedTokens")]
        ok = not broken and bool(editor_visual) and all(v.get("passed") is True for v in editor_visual)
        out.append(entry("G-editor", "passed" if ok else "failed", pages=len(editor),
                         worst=max(diffs(editor_visual), default=None),
                         failed=[key_of_slug(e) for e in broken],
                         not_run=["editor-visual"] if editor and not editor_visual else []))

    trips = [e for e in editor if isinstance(e.get("roundtrip"), dict)]
    new_post, new_page = r.get("newPost"), r.get("newPage")
    # What the editor's first save writes back (R.serialization[]): a row
    # that does not write the stored markup back unchanged fails the gate,
    # whether or not the save/reload ran. A page, post or product is its
    # page key; a template or part the literal template-<slug> / part-<slug>.
    serial = [s for s in lst(r.get("serialization")) if isinstance(s, dict)]
    unsaved = [s for s in serial if s.get("passed") is not True]
    # What another theme shows of a page (R.withoutTheme[]; its passed says
    # whether the saved HTML shows what the theme does, bound values aside).
    # A row that fails fails the gate the same way, by its page key.
    unsaved += [s for s in lst(r.get("withoutTheme")) if isinstance(s, dict) and s.get("passed") is not True]
    def key_of_row(row):
        if row.get("kind") in ("templates", "template-parts"):
            slug = re.sub(r"[^a-z0-9-]+", "-", str(row.get("id") or "").rsplit("//", 1)[-1].lower()).strip("-")
            return (("template-" if row.get("kind") == "templates" else "part-") + slug) if slug else None
        return key_of_slug(row)
    if not trips and new_post is None and not unsaved:
        out.append(entry("G-roundtrip", "not-run", not_run=aborted))
    else:
        bad = [e for e in trips if e["roundtrip"].get("invalid") or e["roundtrip"].get("unknown")
               or e["roundtrip"].get("textPersisted") is not True]
        needs_page = r.get("contractSchema") == "h2wp-blocks/2"
        post_ok = isinstance(new_post, dict) and new_post.get("passed") is True
        page_ok = not needs_page or (isinstance(new_page, dict) and new_page.get("passed") is True)
        failed = [key_of_slug(e) for e in bad] + [key_of_row(s) for s in unsaved]
        if isinstance(new_post, dict) and not post_ok: failed.append("new-post")
        if needs_page and isinstance(new_page, dict) and not page_ok: failed.append("new-page")
        missing = (["new-post"] if new_post is None else []) + (["new-page"] if needs_page and new_page is None else [])
        ok = not bad and not unsaved and post_ok and page_ok
        out.append(entry("G-roundtrip", "passed" if ok else "failed", pages=len(trips),
                         failed=failed, not_run=missing))

    imp, prev = r.get("import"), r.get("preview")
    if imp is None and prev is None:
        out.append(entry("G-import", "not-run", not_run=aborted))
    else:
        ok = (isinstance(imp, dict) and imp.get("passed") is True
              and isinstance(prev, dict) and prev.get("passed") is True)
        out.append(entry("G-import", "passed" if ok else "failed",
                         not_run=(["import"] if imp is None else []) + (["preview"] if prev is None else [])))
    return out


manifest = read("conversion-manifest.json") or {}
gutenberg = manifest.get("schema") == "html2wp/2" or manifest.get("target") == "gutenberg"
common = [
    gate("A",  read("verify-static", "report.json")),
    gate("A2", read("parity-report.json") or read("verify-parity", "report.json")),
]
woo = gate("woo-coverage", read("woo-coverage", "report.json"))
if gutenberg:
    # A v2 job never runs B, C or the Visual Edit smoke test: it reports the
    # local verification's own gates, which the service accepts for it.
    gates = common + gutenberg_gates(manifest) + [woo]
else:
    wp = read("verify-wp", "report.json")
    gates = common + [
        gate("B",  wp),
        gate("C",  wp),
        gate("smoke-editor", read("smoke-editor", "report.json")),
        woo,
    ]

payload = {"job": job, "gates": gates}
if outcome:
    payload["outcome"] = outcome
json.dump(payload, sys.stdout, indent=2)
PY

python3 - "$TMP/payload.json" <<'PY'
import json, sys
p = json.load(open(sys.argv[1]))
for g in p["gates"]:
    extra = []
    if "worstPct" in g: extra.append(f"{g['worstPct']}%")
    if g.get("failedKeys"): extra.append("failed: " + ", ".join(g["failedKeys"]))
    print("   %-14s %-8s %s" % (g["gate"], g["verdict"], "  ".join(extra)))
PY

if [ "$DRY" = "1" ]; then
  echo; echo "--dry-run: not sent. This is the whole payload:"; cat "$TMP/payload.json"; exit 0
fi

# Bounded hard: this runs AFTER the theme is in the user's hands, so it must
# never be the thing that makes a finished conversion look stuck.
# The token goes to curl in a header file, not on its command line.
(umask 077; printf 'authorization: Bearer %s\n' "$TOKEN" > "$TMP/auth.headers")
HTTP="$(curl -sS --connect-timeout 10 --max-time 30 \
  -o "$TMP/reply.json" -w '%{http_code}' -X POST "$API/v1/verdicts" \
  -H "x-html2wp-client: ${CLIENT_VERSION:-unknown}" \
  -H "x-html2wp-host: $CLIENT_HOST" \
  -H 'content-type: application/json' -H @"$TMP/auth.headers" \
  --data-binary @"$TMP/payload.json" || echo 000)"
if [ "$HTTP" = "200" ]; then
  echo "reported"
  # cleanup.sh refuses to delete the gate reports until this exists — the
  # service needs them, and a caller who owes verdicts is refused their NEXT
  # conversion.
  : > "$WS/.h2wp-verdicts-sent"
else
  # Never a stop: the conversion is finished either way, and the next job will
  # ask again. Say what happened so it is not a silent omission.
  echo "could not report the gates (HTTP $HTTP) — the next conversion will ask for them again" >&2
  [ -s "$TMP/reply.json" ] && head -c 400 "$TMP/reply.json" >&2 && echo >&2
fi
