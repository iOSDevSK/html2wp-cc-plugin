#!/usr/bin/env python3
"""Stage 2.65 — give every submittable form control a stable `name`.

  python3 normalize-form-fields.py --manifest conversion-manifest.json [--apply]

A component-driven form (React/Vue/shadcn/Radix, anything built around
controlled inputs) routinely ships each control with only an `id` — the
framework never needs a `name`, it reads/writes the DOM node directly. A
browser's native form submission does not care about `id`: it serializes
ONLY fields that carry a `name`. The converted page renders pixel-identical
and the Visual Edit form connector wires up cleanly (see the `[wp-form]`
contract in SKILL.md's gotchas), but every submission arrives with an EMPTY
body — nothing else in the pipeline can see this: the field is visibly
there, gate A matches (both sides render the same unnamed input), and only
a real submit-and-inspect-the-payload test would catch it. Verified in a
real conversion's post-mortem (skill-to-do.md #5): a React contact form's
fields all had `id` and none had `name`.

Why this reads `dist/` but writes `src/fragments/`: the built dist/ tree is
what actually has to be inspected for the bug (an un-rebuilt fragment can
still look fine), but dist/ is a `npm run build` OUTPUT — the very next
build throws away anything patched only there. The fix has to land in the
Astro project's OWN source fragments (`src/fragments/bodies/{key}.html` for
a page's own markup, `src/fragments/chrome/*.html` for a form that lives in
a shared header/footer/drawer) so it survives a rebuild. This mirrors
materialize-js-text.py (stage 2.6), which hits the same dist-vs-fragment
split for the same reason.

Algorithm, no framework assumptions:

  1. Parse every built page in `dist/` (a plain HTMLParser walk — no browser
     needed, nothing here depends on JS having run) and collect every
     submittable control: `<input>` except type=button/submit/reset/image,
     every `<select>`, every `<textarea>`.
  2. Skip anything that already carries a non-empty `name` — this IS the
     idempotency guarantee: a field this script named on a prior run is
     indistinguishable, on the next run, from one the site's own author
     named, and both are left alone.
  3. For everything left, derive a name in this order (first that produces
     a non-empty slug wins): the field's own `id` -> `aria-label` ->
     `placeholder` -> the text of a `<label>` it is nested inside (the
     `<label>…<input>…</label>` wrap pattern) -> a positional fallback
     (`{tag}-{form-index}-{control-index}`), which is flagged loudly rather
     than applied quietly (see REFUSE/WARN below). A `<label for="…">`
     pointing at this field's `id` is not a separate branch: whenever `id`
     is present it already won at step 1, so a for="" lookup would only
     ever fire for a field this script has no id to key it by in the first
     place.
  4. Radio buttons are a special case: multiple unnamed radios are usually
     ONE logical group meant to share a single `name` (their `value`
     distinguishes the choice) — deriving each independently would give
     every radio its own name and break the group. The shared name comes
     from an enclosing `<fieldset><legend>` or an enclosing
     `[role="radiogroup"]`'s own `aria-label`. Absent either, this script
     REFUSES rather than guess whether a set of same-named-nothing radios
     was meant to share a name or not — see `status: "ambiguous"` in the
     report.
  5. A derived name that collides with another field already named in the
     SAME `<form>` (two fields whose only clue was an identical placeholder,
     say) is auto-disambiguated with a `-2`, `-3`, … suffix — deterministic
     and safe to write, but always reported, never silent.
  6. To write the change, this locates the control's own opening tag
     (verbatim, exactly as materialize-js-text.py locates an empty
     element's opening tag) inside the fragment file(s) and inserts
     `name="…"` into it. A shared chrome fragment referenced by several
     pages is patched exactly once — the first page that needs it finds and
     fixes the fragment; every later page that resolves to the same
     already-patched fragment is reported as "already named" instead of
     patched again.

Without --apply nothing is written; the report alone is worth running.
Exit code: 0 when every unnamed field the report lists was either applied
cleanly or was already named; 1 only when something was left `ambiguous` or
`not uniquely locatable` — a gate verdict on whether a human needs to look,
not on whether the script itself crashed.
"""

import argparse, json, re, sys
from html.parser import HTMLParser
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--manifest", required=True)
ap.add_argument("--apply", action="store_true",
                help="write the changes; without it, report only")
ap.add_argument("--out", default=None,
                help="report path; defaults into the manifest's workspace, "
                     "which is where gate A2 reads it from")
args = ap.parse_args()

MF = json.loads(Path(args.manifest).read_text())
WS = Path(MF["workspace"]).resolve()
# This report is the DERIVED-EXEMPTION record gate A2 reverses this stage's
# edits from, and verify-parity.mjs reads it at {workspace}/<name>.json. A
# cwd-relative default drops it wherever the script was invoked from — for a
# skill run out of its own checkout, never the workspace — and gate A2 then
# reads the added name= attributes as unexplained drift and fails.
args.out = args.out or str(WS / "normalize-form-fields-report.json")
DIST = WS / "astro-project" / "dist"
FRAG_BODIES = WS / "astro-project" / "src" / "fragments" / "bodies"
FRAG_CHROME = WS / "astro-project" / "src" / "fragments" / "chrome"

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr"}
SUBMITTABLE = {"input", "select", "textarea"}
SKIP_INPUT_TYPES = {"button", "submit", "reset", "image"}
# Framework-generated ids (Radix/MUI/Headless UI/React useId, sequential
# hex/uuid chunks) are still STABLE within one build — good enough to name a
# field — but rarely mean anything to a human reading Form Submissions
# later, so a field named from one of these is flagged, not refused.
AUTOGEN_ID_RE = re.compile(r"^(radix-|mui-|headlessui-|:r)|[0-9a-f]{6,}$|^\d+$", re.I)


def slugify(text, maxlen=48):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s[:maxlen].strip("-")


class FormScanner(HTMLParser):
    """One pass over a built page: every submittable control, with enough
    context (form index, nesting order, nearest wrapping <label>/<fieldset>
    text) to derive a name and to know whether it already has one."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []          # [{index, controls: [...]}]
        self._form_stack = []    # indices into self.forms currently open
        self._label_stack = []   # list of [text chunks] for open <label>s
        self._fieldset_stack = []  # [{legend: [chunks], capturing_legend: bool, radiogroup: str|None}]
        self._control_ordinal = 0

    def _attrs_dict(self, attrs):
        return {k.lower(): (v or "") for k, v in attrs}

    def _open_control(self, tag, attrs):
        if not self._form_stack:
            return  # a submittable control outside any <form> submits nowhere relevant here
        a = self._attrs_dict(attrs)
        if tag == "input" and a.get("type", "text").lower() in SKIP_INPUT_TYPES:
            return
        ctype = a.get("type", "text").lower() if tag == "input" else ("" if tag == "select" else "textarea")
        radiogroup_label = None
        for fs in reversed(self._fieldset_stack):
            if fs.get("radiogroup"):
                radiogroup_label = fs["radiogroup"]
                break
            legend = "".join(fs["legend"]).strip()
            if legend:
                radiogroup_label = legend
                break
        self._control_ordinal += 1
        form = self.forms[self._form_stack[-1]]
        form["controls"].append({
            "tag": tag,
            "type": ctype,
            "attrs": a,
            "raw": self.get_starttag_text(),
            "already_named": bool(a.get("name", "").strip()),
            "implicit_label_text": ("".join(self._label_stack[-1]).strip() if self._label_stack else ""),
            "radiogroup_label": radiogroup_label,
            "control_ordinal": self._control_ordinal,
        })

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "form":
            self.forms.append({"index": len(self.forms), "controls": [],
                                "open_tag": self.get_starttag_text()})
            self._form_stack.append(len(self.forms) - 1)
            return
        if tag == "label":
            self._label_stack.append([])
        if tag == "fieldset":
            self._fieldset_stack.append({"legend": [], "capturing_legend": False, "radiogroup": None})
        if tag in ("fieldset", "div", "section", "span") and self._fieldset_stack:
            a = self._attrs_dict(attrs)
            if a.get("role", "").lower() == "radiogroup":
                label = a.get("aria-label", "")
                if label:
                    self._fieldset_stack[-1]["radiogroup"] = label
        if self._fieldset_stack and tag == "legend":
            self._fieldset_stack[-1]["capturing_legend"] = True
        if tag in SUBMITTABLE:
            self._open_control(tag, attrs)
        if tag in VOID or tag == "input":
            return  # no matching end tag to track

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if tag in SUBMITTABLE:
            self._open_control(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "form" and self._form_stack:
            self._form_stack.pop()
        elif tag == "label" and self._label_stack:
            self._label_stack.pop()
        elif tag == "legend" and self._fieldset_stack:
            self._fieldset_stack[-1]["capturing_legend"] = False
        elif tag == "fieldset" and self._fieldset_stack:
            self._fieldset_stack.pop()

    def handle_data(self, data):
        if self._label_stack:
            self._label_stack[-1].append(data)
        if self._fieldset_stack and self._fieldset_stack[-1]["capturing_legend"]:
            self._fieldset_stack[-1]["legend"].append(data)


def derive_name(control, form_index):
    a = control["attrs"]
    if control["tag"] == "input" and control["type"] == "radio":
        if control["radiogroup_label"]:
            return slugify(control["radiogroup_label"]), "radio-group-legend", None
        return None, "ambiguous", "radio with no name, no <fieldset><legend> and no [role=radiogroup] aria-label — refusing to guess whether these radios share a group"
    if a.get("id", "").strip():
        name = slugify(a["id"])
        note = "id looks framework-auto-generated (stable, but not human-meaningful — consider a real name in code)" if AUTOGEN_ID_RE.search(a["id"]) else None
        return name, "id", note
    if a.get("aria-label", "").strip():
        return slugify(a["aria-label"]), "aria-label", None
    if a.get("placeholder", "").strip():
        return slugify(a["placeholder"]), "placeholder", None
    if control["implicit_label_text"]:
        return slugify(control["implicit_label_text"]), "label(wrap)", None
    return f"{control['tag']}-{form_index + 1}-{control['control_ordinal']}", "positional", \
        "no id/aria-label/placeholder/wrapping <label> found — positional name is stable but tells the owner nothing; rename in code if this field matters"


def inject_name(open_tag, tag, name):
    attr = f' name="{name}"'
    if tag in VOID or tag == "input":
        # `<input ... />` or `<input ...>` — insert before the trailing
        # `/>` or `>`, never after (attribute order inside an open tag is
        # cosmetic, but inserting after `>` would corrupt the tag).
        if open_tag.rstrip().endswith("/>"):
            return open_tag.rstrip()[:-2].rstrip() + attr + " />"
        return open_tag.rstrip()[:-1] + attr + ">"
    return open_tag.rstrip()[:-1] + attr + ">"


def fragment_candidates(page_key, file_stem=None):
    # html-to-astro.mjs names a body fragment after the page's FILE PATH minus
    # .html — bodies/contact/index.html for contact/index.html — not after its
    # manifest `key`, and not after the basename either: a directory-routed
    # export (Next.js/Astro/Hugo) gives EVERY page the basename stem "index",
    # so a basename lookup asks for bodies/index.html on all of them and every
    # form field on every page came back "not uniquely locatable", blaming a
    # stale dist/ for a filename the builder never writes. Callers pass the
    # path stem; for a flat site it is identical to the basename. The two agree
    # for most pages and NEVER for the front page, whose key is `front-page`
    # by the manifest's own convention while its file is `index.html` — so
    # every field of a front-page form came back "not uniquely locatable",
    # blaming a stale dist/ for a lookup that was simply asking for a
    # filename the builder does not write. Try both spellings.
    cands = []
    for stem in [page_key, file_stem]:
        if not stem:
            continue
        body = FRAG_BODIES / f"{stem}.html"
        if body.exists() and body not in cands:
            cands.append(body)
    if FRAG_CHROME.exists():
        cands.extend(sorted(FRAG_CHROME.glob("*.html")))
    # A SELF-CONTAINED page never becomes a fragment: html-to-astro copies it
    # into public/ verbatim, so it has no bodies/{stem}.html and every one of
    # its fields came back "not locatable — dist/ may be stale", which names
    # the wrong file. public/{file} IS that page's build source, exactly as
    # bodies/{stem}.html is a consensus page's, so patching it there is the
    # same operation and survives the next build the same way. Login and
    # signup pages are the common instance, and they are all form.
    if file_stem:
        pub = WS / "astro-project" / "public" / f"{file_stem}.html"
        if pub.exists() and pub not in cands:
            cands.append(pub)
    return cands


# In-memory working copy of every fragment file this run touches. Reading
# through this (rather than the disk file) on every attempt, and mutating it
# in place on a successful match, is what makes "the same byte-identical
# open tag appears twice in one fragment" (two bare `<input placeholder="Company">`
# fields) resolve correctly: the first control's patch consumes one
# occurrence from the cache, so the second control's attempt — with the same
# search string but a DIFFERENT derived name — finds only the still-bare
# occurrence left over and lands on it, in document order. Applies equally
# in dry-run: nothing is written to disk, but the report still reflects each
# occurrence being consumed once rather than the same stale match twice.
frag_cache = {}


def frag_text(path):
    if path not in frag_cache:
        frag_cache[path] = path.read_text() if path.exists() else None
    return frag_cache[path]


def try_patch(path, old_open_tag, new_open_tag):
    text = frag_text(path)
    if text is None or old_open_tag not in text:
        return False
    frag_cache[path] = text.replace(old_open_tag, new_open_tag, 1)
    return True


report = {"applied": bool(args.apply), "pages": {}, "summary": {}}
# Every raw-open-tag string this run has SUCCESSFULLY resolved, in the order
# resolved: (fragment_path, name). Consulted only when try_patch finds no
# remaining bare occurrence anywhere — i.e. this exact control markup was
# already handled earlier in this run. That happens for a genuinely SHARED
# fragment (a footer form appearing on every page): page 1 consumes the
# fragment's one occurrence for real; page 2's identical control has nothing
# left to consume, so it is reported against page 1's already-applied name
# instead of a spurious "not locatable".
success_log = {}
counts = {"named": 0, "already_named": 0, "ambiguous": 0, "not_locatable": 0}

for entry in MF["pages"]:
    f = entry["file"]
    key = entry.get("key", Path(f).stem)
    src = DIST / f
    if not src.exists():
        continue
    scanner = FormScanner()
    scanner.feed(src.read_text(errors="replace"))
    if not scanner.forms:
        continue

    page_note = {"forms": []}
    for form in scanner.forms:
        if not form["controls"]:
            continue
        # name -> "radio-group" | "field". A plain set can't tell "two
        # radios that are SUPPOSED to share one name" (not a collision, the
        # whole point of a group) apart from "two unrelated fields that
        # happened to derive the same slug" (a real collision, needs
        # disambiguating) — the kind tells them apart.
        used_names = {}
        form_note = {"formIndex": form["index"], "openTag": form["open_tag"][:120], "fields": []}
        for control in form["controls"]:
            field_note = {"tag": control["tag"], "type": control["type"] or None,
                          "attrs": {k: v for k, v in control["attrs"].items() if k in ("id", "name", "type", "aria-label", "placeholder")}}
            if control["already_named"]:
                field_note["status"] = "already named"
                field_note["name"] = control["attrs"]["name"]
                counts["already_named"] += 1
                form_note["fields"].append(field_note)
                continue

            name, source, note = derive_name(control, form["index"])
            if name is None:
                field_note["status"] = "ambiguous"
                field_note["reason"] = note
                counts["ambiguous"] += 1
                form_note["fields"].append(field_note)
                continue

            is_radio_group_member = (source == "radio-group-legend")
            if is_radio_group_member and used_names.get(name) == "radio-group":
                pass  # a later member of a group already opened by an earlier radio — reuse verbatim, no suffix
            else:
                base_name = name
                n = 2
                collided = name in used_names
                while name in used_names:
                    name = f"{base_name}-{n}"
                    n += 1
                used_names[name] = "radio-group" if is_radio_group_member else "field"
                if collided:
                    note = (note + " | " if note else "") + \
                        f"collided with a sibling field's derived name ({base_name}) in the same <form> — auto-disambiguated"
            field_note["name"] = name
            field_note["derivedFrom"] = source
            if note:
                field_note["note"] = note

            new_open_tag = inject_name(control["raw"], control["tag"], name)
            patched = False
            location = None
            for frag in fragment_candidates(key, str(Path(f).with_suffix(""))):
                if try_patch(frag, control["raw"], new_open_tag):
                    patched = True
                    location = frag
                    break
            if patched:
                success_log.setdefault(control["raw"], []).append((location, name))
                field_note["status"] = "named" if args.apply else "would name"
                field_note["fragment"] = str(location)
                # The exact tag text before and after. Naming a field is a
                # DELIBERATE transformation of the markup, so gate A2's byte
                # comparison against the source would otherwise fail on it —
                # and the honest way to exempt it is to let the gate reverse
                # precisely this edit from the record, rather than to stop
                # running the gate on the final build. Same contract
                # materialize-js-text.py's openFull/tag serve.
                field_note["openBefore"] = control["raw"]
                field_note["openAfter"] = new_open_tag
                counts["named"] += 1
            elif control["raw"] in success_log:
                # Not found because an earlier page already consumed the one
                # (shared-fragment) occurrence for real — not because the
                # data is broken. Distinct from "already named": the SITE'S
                # OWN author never touched this field; we did, one page ago.
                prior_location, prior_name = success_log[control["raw"]][-1]
                field_note["status"] = "already handled (shared fragment)"
                field_note["name"] = prior_name
                field_note["fragment"] = str(prior_location)
                # The edit is in THIS page's built output too — the fragment
                # is shared, so every page that includes it renders the name.
                # Gate A2 compares per page, so it needs the before/after pair
                # here as much as on the page that did the patching; without it
                # the first page is exempt and the other six fail on a footer
                # they all legitimately share.
                field_note["openBefore"] = control["raw"]
                field_note["openAfter"] = inject_name(control["raw"], control["tag"], prior_name)
                field_note["note"] = (field_note.get("note", "") + " | " if field_note.get("note") else "") + \
                    "shared with a fragment already patched earlier this run"
                counts["named"] += 1
            else:
                field_note["status"] = "not uniquely locatable"
                field_note["reason"] = "control's opening tag not found verbatim in any candidate fragment (bodies/ or chrome/) — dist/ may be stale, rebuild and retry"
                counts["not_locatable"] += 1
            form_note["fields"].append(field_note)
        page_note["forms"].append(form_note)
    if page_note["forms"]:
        report["pages"][f] = page_note

if args.apply:
    for path, text in frag_cache.items():
        if text is not None:
            path.write_text(text)

# The SCRIPT is idempotent — a field that already carries a name is skipped —
# but its REPORT was not, and the report is load-bearing: gate A2 reverses
# exactly the edits recorded here before comparing. A second, no-op run
# therefore used to overwrite a real record with one where every field reads
# "already named" and nothing carries a before/after pair, and the next gate A2
# failed on the very transformation the record existed to account for. Prior
# edits are carried forward whenever their patched tag is still on the page.
# Same contract, same fix as materialize-js-text.py's `carried`.
prev_path = Path(args.out)
if prev_path.exists():
    try:
        prev = json.loads(prev_path.read_text())
    except (OSError, ValueError):
        prev = None
    if prev and prev.get("applied"):
        for file, note in (prev.get("pages") or {}).items():
            here = report["pages"].setdefault(file, {"forms": []})
            have = {d.get("openAfter") for form in here["forms"] for d in form.get("fields", [])}
            kept = [d for form in (note.get("forms") or []) for d in (form.get("fields") or [])
                    if d.get("openBefore") and d.get("openAfter") and d["openAfter"] not in have]
            if kept:
                here["forms"].append({"formIndex": None, "openTag": "(carried forward)",
                                      "carriedForward": True,
                                      "fields": [dict(d, carriedForward=True) for d in kept]})

report["summary"] = counts
Path(args.out).write_text(json.dumps(report, indent=2))

verb = "named" if args.apply else "would name"
print(f"{verb} {counts['named']} field(s), {counts['already_named']} already named, "
      f"{counts['ambiguous']} ambiguous (refused), {counts['not_locatable']} not locatable "
      f"across {len(report['pages'])} page(s) with forms -> {args.out}")
for pg, note in report["pages"].items():
    for form in note["forms"]:
        for fld in form["fields"]:
            if fld["status"] in ("ambiguous", "not uniquely locatable"):
                print(f"    REFUSED {pg} <{fld['tag']}>: {fld.get('reason')}")
if args.apply and counts["named"]:
    print("  Rebuild (npm run build) before running gate A / any later stage: "
        "the fix lives in src/fragments/, not in dist/.")

sys.exit(1 if (counts["ambiguous"] or counts["not_locatable"]) else 0)
