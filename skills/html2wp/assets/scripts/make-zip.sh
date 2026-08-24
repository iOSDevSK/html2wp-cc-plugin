#!/usr/bin/env bash
# Stage 6 — package the generated theme as the installable deliverable.
#
#   make-zip.sh <theme-dir> <output.zip>
#
# The ZIP carries EXACTLY what a WordPress install needs: the theme files and
# the clara-content/ bundle riding inside it. Junk and build residue are
# excluded, and the archive root is the theme's directory name — WordPress
# derives the theme's identity from it.
#
# Refuses to package a theme whose PHP does not lint. Warns (not fails) on a
# missing screenshot.png before verification has produced one, because the
# install→verify→screenshot→re-zip order is circular by design: the real
# screenshot is captured from the live site, so the FIRST zip legitimately
# lacks it and the FINAL one must not.
set -euo pipefail

SRC="${1:?usage: make-zip.sh <theme-dir> <output.zip>}"
OUT="${2:?usage: make-zip.sh <theme-dir> <output.zip>}"
SRC="$(cd "$SRC" && pwd)"
NAME="$(basename "$SRC")"
# Resolve the output to an absolute path BEFORE the subshell cd's into the
# staging dir. A relative path would write the archive inside that temp dir,
# which the EXIT trap then deletes — leaving a "built …" line, exit 0, and
# no file anywhere.
case "$OUT" in /*) ;; *) OUT="$PWD/$OUT" ;; esac
mkdir -p "$(dirname "$OUT")"

# lint every PHP file — a broken theme must not become a deliverable
while IFS= read -r -d '' f; do
  php -l "$f" >/dev/null || { echo "PHP syntax error: $f" >&2; exit 1; }
done < <(find "$SRC" -name '*.php' -print0)

[ -f "$SRC/style.css" ] || { echo "missing style.css" >&2; exit 1; }
[ -f "$SRC/theme.json" ] || { echo "missing theme.json" >&2; exit 1; }

# Re-running make-theme WIPES the theme directory, clara-content included, so
# a bundle copied in before that run is gone by the time this packages it.
# Nothing downstream notices: the ZIP installs, the pages import, and the site
# comes up empty — verified live, where a posts.json that was correct in
# bundle-out/ was 2 bytes inside the theme and the first import produced no
# blog at all. The re-copy discipline was prose in SKILL.md, and prose is not
# a check. This is.
if [ ! -d "$SRC/clara-content" ]; then
  echo "refusing: no clara-content/ in the theme — the import would land an empty site." >&2
  echo "  make-theme wipes the theme directory; re-copy the bundle after EVERY make-theme run:" >&2
  echo "    cp -R {workspace}/bundle-out/clara-content \"$SRC/\"" >&2
  exit 1
fi
SOURCES_N="$(find "$SRC/clara-content/sources" -type f -name '*.html' 2>/dev/null | wc -l | tr -d ' ')"
if [ "${SOURCES_N:-0}" -eq 0 ]; then
  echo "refusing: clara-content/sources/ holds no page sources — the bundle is empty or truncated." >&2
  echo "  re-copy it after make-theme and check the sizes match bundle-out/" >&2
  exit 1
fi
# With the conversion manifest the count becomes checkable rather than merely
# non-zero, and the blog case — the one that actually shipped — visible too.
# Optional so the signature stays <theme-dir> <output.zip>.
if [ -n "${MAKE_ZIP_MANIFEST:-}" ] && [ -f "$MAKE_ZIP_MANIFEST" ] && command -v python3 >/dev/null 2>&1; then
  python3 - "$MAKE_ZIP_MANIFEST" "$SRC/clara-content" "$SOURCES_N" <<'PY' || exit 1
import json, sys
mf = json.load(open(sys.argv[1]))
bundle, got = sys.argv[2], int(sys.argv[3])
# Articles become Posts, so they are deliberately absent from sources/.
# So is anything stage 4 was told to leave out with --exclude-page: an input
# can hold an .html file that is not a document (a scrape's partials/*.html),
# and the operator says so once, on the bundler's command line. Repeat the
# keys in MAKE_ZIP_EXCLUDED so this count still means something instead of
# being switched off wholesale.
import os
excluded = {k.strip() for k in os.environ.get("MAKE_ZIP_EXCLUDED", "").split(",") if k.strip()}
# Products are absent from sources/ for the same reason articles are — they
# become records. The cart and the checkout are absent for the opposite one:
# WooCommerce owns those pages and the conversion deliberately ships neither.
shop = mf.get("shop", {})
commerce_keys = set()
if shop.get("present"):
    for _f in (shop.get("cartPage"), shop.get("checkoutPage")):
        if not _f:
            continue
        _k = next((p.get("key") for p in mf.get("pages", []) if p.get("file") == _f), None)
        if _k:
            commerce_keys.add(_k)
want = len([p for p in mf.get("pages", [])
            if p.get("kind") not in ("article", "product")
            and p.get("key") not in excluded
            and p.get("key") not in commerce_keys])
if got < want:
    print(f"refusing: clara-content/sources/ has {got} page sources, the manifest declares "
          f"{want} non-article pages — the bundle is stale or was wiped by a make-theme re-run",
          file=sys.stderr)
    sys.exit(1)
blog = mf.get("blog", {})
if blog.get("present"):
    try:
        posts = json.load(open(f"{bundle}/posts.json"))
    except Exception:
        posts = []
    # The thing being caught is stage 4.5 never running, or a make-theme
    # re-run wiping what it produced. "posts.json is empty" was a stand-in
    # for that, and it is wrong for the case the blog rule now creates on
    # purpose: a template that ships a blog listing with NO article pages
    # behind it. Wiring that listing is the documented decision — the cards
    # link to '#', so a static copy of them is the template's own lie — and
    # blog.articles is then legitimately [], which no amount of re-running
    # stage 4.5 can change. Refusing there fails a correctly wired blog for
    # having told the truth, and the only "fix" would be to invent articles.
    #
    # So compare against what the manifest actually declares (a wipe still
    # shows up as fewer posts than articles), and prove the OTHER half of
    # stage 4.5 directly: the listing's own stored source must carry the
    # [wp-posts] token. That check holds for zero articles and for fifty,
    # and it is the one that says the listing is live rather than static.
    want_posts = len(blog.get("articles") or [])
    if len(posts) < want_posts:
        print(f"refusing: the manifest declares {want_posts} article(s) and clara-content/posts.json "
              f"has {len(posts)} — the listing would import with nothing to list (stage 4.5 never "
              "ran, or a later make-theme wiped its output)", file=sys.stderr)
        sys.exit(1)
    listing_file = blog.get("listing") or ""
    key = next((p.get("key") for p in mf.get("pages", []) if p.get("file") == listing_file), None)
    if key:
        try:
            listing_src = open(f"{bundle}/sources/{key}.html", encoding="utf-8").read()
        except Exception:
            listing_src = ""
        if "[wp-posts" not in listing_src:
            print(f"refusing: the manifest declares a blog but clara-content/sources/{key}.html "
                  "carries no [wp-posts] token — the listing would import as the static cards the "
                  "source shipped, so the owner's first post would never appear on it (stage 4.5 "
                  "never ran, or a later make-theme wiped its output)", file=sys.stderr)
            sys.exit(1)

    # mc-004, as a refusal rather than a person's job. It failed 5 of 8 sites
    # in the finishing pass — the campaign's most frequent bridge failure —
    # and it has always had a deterministic shape: does the shipped article
    # layout use class names this site actually defines?
    #
    # The dangerous case is narrow. When buildArticlePart() cannot derive a
    # layout (the source article has no <main> or no <article>, which a
    # hand-written template routinely lacks), make-theme ships the GENERIC
    # default and warns. Nothing goes red: gate B1 does not render article
    # pages, C3 checks post counts and headline text, and unstyled markup still
    # renders. Every post then arrives in a foreign skeleton.
    #
    # It refuses only when the site HAS posts — a template with a blog listing
    # and no articles legitimately ships the default, and failing that would be
    # failing a correct conversion for telling the truth.
    # theme-report.json lives in the WORKSPACE, and the bundle sits at
    # workspace/theme/<slug>/clara-content — so the depth between them depends
    # on the slug. Walk up rather than hard-code it: a hard-coded "../" reads
    # as "no report", which silently disables the refusal, which is the exact
    # failure mode this batch exists to end.
    import os
    tr = {}
    probe = os.path.abspath(bundle)
    for _ in range(5):
        probe = os.path.dirname(probe)
        cand = os.path.join(probe, "theme-report.json")
        if os.path.exists(cand):
            try:
                tr = json.load(open(cand))
            except Exception:
                tr = {}
            break
    ap = tr.get("articlePart") or {}
    cov = (ap.get("classCoverage") or {}).get("share")
    undef_sample = (ap.get("classCoverage") or {}).get("undefinedSample") or []
    # hotfix (sidefolio-010): measure the ARTIFACT, not the snapshot. The
    # refusal below tells you to restyle parts/article.html by hand — which is
    # exactly what stage 3 instructs when the derivation fails — and then keeps
    # refusing, because theme-report.json describes the GENERIC DEFAULT that
    # make-theme emitted, and no later step rewrites it. So the prescribed
    # remedy could not clear the gate. Same computation as make-theme's
    # articleClassCoverage(), run against the file on disk.
    import glob, re as _re
    _part = os.path.join(bundle, os.pardir, "parts", "article.html")
    if os.path.exists(_part):
        _css = ""
        for _f in glob.glob(os.path.join(bundle, os.pardir, "**", "*.css"), recursive=True):
            try:
                _css += open(_f, encoding="utf-8", errors="ignore").read()
            except Exception:
                pass
        _klass = set()
        for _m in _re.finditer(r'class="([^"]*)"', open(_part, encoding="utf-8").read()):
            _klass.update(x for x in _m.group(1).split() if x)
        if _klass:
            _undef = sorted(c for c in _klass if ("." + c) not in _css)
            cov = round((len(_klass) - len(_undef)) / len(_klass), 3)
            undef_sample = _undef[:8]
    if want_posts and cov is not None and cov < 0.5:
        undef = ", ".join(undef_sample)
        why = ("the layout could not be derived from the source article page, so the generic "
               "default shipped") if not ap.get("derived") else "the derived layout does not match this site"
        pct = int(round(cov * 100))
        print("refusing: parts/article.html uses class names this site does not define "
              f"({pct}% of its classes appear in the site's CSS; undefined include: {undef}) — "
              f"{why}, so every one of the {want_posts} posts would render in a foreign skeleton. "
              "Restyle parts/article.html from the site's own article page, or point the derivation "
              "at a page that has both <main> and <article>.", file=sys.stderr)
        sys.exit(1)

    # mc-023 / creative-014. parts/article.html tokenizes the byline as
    # [wp-article field="author"] — correctly, because the source article HAS a
    # byline — but posts.json rows carried no author, so wp_insert_post falls
    # back to whoever ran the import and every post reads "admin" where the
    # source names its writer. C3 checks counts and headlines; nothing reads a
    # byline. The refusal is conditional on the part actually rendering an
    # author, so a design with no byline ships unchanged.
    if want_posts:
        try:
            part = open(f"{bundle}/../parts/article.html", encoding="utf-8").read()
        except Exception:
            part = ""
        if 'field="author"' in part:
            noauthor = [p.get("title", "?") for p in posts if not (p.get("author") or "").strip()]
            if noauthor:
                print("refusing: parts/article.html renders [wp-article field=\"author\"] but "
                      f"{len(noauthor)} of {len(posts)} posts.json row(s) carry no author "
                      f"({', '.join(noauthor[:4])}) — every such post would show the importing "
                      "user's login as its byline. The source articles have bylines (that is where "
                      "the token came from); carry them: posts.json rows need an \"author\" field.",
                      file=sys.stderr)
                sys.exit(1)

# ---- the shop's own refusals ----
#
# Same failure being caught as the blog's: stage 4.6 never running, or a
# make-theme re-run wiping what it produced. A shop is worse when it happens,
# because the outcome is not an empty listing but a shop that looks complete
# and is missing stock nobody counted.
if shop.get("present"):
    declared = shop.get("products") or []
    try:
        prods = json.load(open(f"{bundle}/products.json"))
    except Exception:
        prods = []
    if len(prods) < len(declared):
        print(f"refusing: the manifest declares {len(declared)} product(s) and clara-content/products.json "
              f"has {len(prods)} — the shop would import a partial catalogue (stage 4.6 never ran, or a "
              "later make-theme wiped its output)", file=sys.stderr)
        sys.exit(1)

    listing_file = shop.get("listing") or ""
    key = next((p.get("key") for p in mf.get("pages", []) if p.get("file") == listing_file), None)
    if key:
        try:
            listing_src = open(f"{bundle}/sources/{key}.html", encoding="utf-8").read()
        except Exception:
            listing_src = ""
        if "[wp-products" not in listing_src:
            print(f"refusing: the manifest declares a shop but clara-content/sources/{key}.html carries no "
                  "[wp-products] token — the listing would import as the static cards the source shipped, "
                  "each linking to a product page that no longer exists (stage 4.6 never ran, or a later "
                  "make-theme wiped its output)", file=sys.stderr)
            sys.exit(1)

    # The product part must exist and must have come from THIS site — the same
    # mc-004 assertion the article part carries, and for a stronger reason:
    # there is no generic product layout to fall back to, so a part that fails
    # this is a WooCommerce page wearing the wrong name.
    _pp = os.path.join(bundle, os.pardir, "parts", "product.html")
    if not os.path.exists(_pp):
        print("refusing: the manifest declares a shop but parts/product.html does not exist — every product "
              "page would fall through to WooCommerce's own template, which is the one place a converted "
              "shop's design would visibly stop", file=sys.stderr)
        sys.exit(1)
    _ptext = open(_pp, encoding="utf-8").read()

    # A product page that cannot be bought from is the defect this whole stage
    # exists to prevent. Nothing else catches it: the page renders, the pixels
    # match, and the button is there — it just posts nowhere.
    if 'field="add-to-cart"' not in _ptext:
        print("refusing: parts/product.html carries no [wp-product field=\"add-to-cart\"] — the product pages "
              "would render perfectly and sell nothing. Name the buy region as shop.addToCart in the manifest "
              "and re-run stage 3.", file=sys.stderr)
        sys.exit(1)

    # A variant chooser with no attribute data behind it is mc-023 for the
    # shop: the template renders a field the records cannot fill, so every
    # product offers sizes that resolve to no variation and the add-to-cart
    # silently fails.
    if shop.get("variantAttribute"):
        # Only VARIATION attributes make this claim. A spec-line attribute
        # ("Materials: 100% wool", visible, variation:false) rides on simple
        # products by design and drives no chooser.
        novar = [p.get("name", "?") for p in prods
                 if any(a.get("variation") for a in p.get("attributes", []))
                 and not p.get("variations")]
        if novar:
            print(f"refusing: {len(novar)} product(s) declare attributes with no variations "
                  f"({', '.join(novar[:4])}) — WooCommerce would offer the chooser and match no variation, "
                  "so add-to-cart fails silently on every one of them", file=sys.stderr)
            sys.exit(1)
PY
fi
# A theme without screenshot.png is a blank checkerboard tile in Appearance →
# Themes, which is how two conversions shipped: this used to be a warning
# because the screenshot came from the live site, so the FIRST zip legitimately
# lacked one and the re-zip after verification was easy to skip. make-screenshot.py
# now shoots the DIST BUILD at stage 3.5, so there is no such moment any more —
# and refusing is the only thing that actually keeps it out of a deliverable.
if [ ! -f "$SRC/screenshot.png" ]; then
  echo "refusing: no screenshot.png — WordPress would show this theme as a blank tile." >&2
  echo "  run: python3 assets/scripts/make-screenshot.py --manifest=conversion-manifest.json" >&2
  exit 1
fi
if command -v python3 >/dev/null 2>&1; then
  python3 - "$SRC/screenshot.png" <<'PY' || exit 1
import sys
try:
    from PIL import Image
except ImportError:
    sys.exit(0)
with Image.open(sys.argv[1]) as im:
    if (im.width, im.height) != (1200, 900):
        print(f"refusing: screenshot.png is {im.width}x{im.height}, WordPress requires 1200x900", file=sys.stderr)
        sys.exit(1)
PY
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/$NAME"
# Junk exclusions are anchored to the theme ROOT (leading /) rather than
# matched at any depth. A converted theme carries the site's own assets at
# their original paths, so an unanchored 'src' or '*.config.js' would drop a
# real asset directory or file the design references. Only build residue
# that can legitimately only exist at the root is removed here; .DS_Store and
# __MACOSX stay unanchored because they are junk wherever they appear.
rsync -a \
  --exclude '.DS_Store' --exclude '__MACOSX' \
  --exclude '/.git*' --exclude '/node_modules' --exclude '/src' \
  --exclude '/package.json' --exclude '/package-lock.json' \
  --exclude '/composer.json' --exclude '/composer.lock' --exclude '/phpcs.xml' \
  --exclude '/*.config.js' --exclude '/.editorconfig' \
  --exclude '/.eslintrc*' --exclude '/.stylelintrc*' \
  "$SRC/" "$STAGE/$NAME/"

rm -f "$OUT"
( cd "$STAGE" && zip -q -r -X "$OUT" "$NAME" )

# Helper scripts and diagnostics are conversion-time tooling, not theme files.
# They reach the theme directory routinely (a weave script run in place, a
# report written next to the parts it describes) and a ZIP is the last place
# to notice. Refused rather than silently excluded: a .py inside a theme means
# something was authored in the wrong directory, and that is worth knowing.
# -Z1 lists ENTRY NAMES only. `unzip -l` prefixes its output with the
# archive's own path, which matched the pattern and failed every clean build.
STRAY="$(unzip -Z1 "$OUT" | grep -E '\.(py|pyc|log|sql)$|(credentials|secret|\.env)' || true)"
if [ -n "$STRAY" ]; then
  echo "refusing: conversion tooling or secrets inside the theme ZIP —" >&2
  echo "$STRAY" | sed 's/^/  /' >&2
  echo "move them to the conversion workspace and rebuild" >&2
  rm -f "$OUT"
  exit 1
fi

if unzip -l "$OUT" | grep -qE '__MACOSX|\.DS_Store|node_modules'; then
  echo "JUNK DETECTED — build is dirty" >&2; exit 1
fi
echo "built $OUT ($(du -h "$OUT" | cut -f1 | tr -d ' ')) — root: $NAME/"
