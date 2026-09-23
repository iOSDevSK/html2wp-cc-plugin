# Direct Gutenberg conversion with local workers

This is the explicit v2 path. AI interpretation runs in the local host; the
service validates and deterministically compiles approved block plans. Old
manifests retain their legacy generator. All conversion acceptance gates apply.

## Prepare and freeze

Complete analysis, prerender, Astro build and static verification. Keep the
input project unchanged and use an isolated conversion workspace. Run:

```sh
node assets/scripts/prepare-block-plan.mjs --manifest={workspace}/conversion-manifest.json
```

This explicitly sets `schema: "html2wp/2"` and `target: "gutenberg"`, writes
`block-plan/contract.json`, `block-plan/pages/<key>.json`, and local inventory,
tasks and checkpoints under `.gutenberg/`. Source scripts are never executed.
Bootstrap proposals are drafts. Unsupported markup/attributes and application
scripts are blocking findings requiring worker review. Never enqueue React
hydration against WordPress content. The prerender's own interaction runtime
(`assets/spa-runtime.js`, recognised by its generated header) is added to
`contract.scripts` automatically, and its entrance animation `<style>`
(scoped per recorded duration) goes to `assets/gutenberg-head.css`. Source
inline `<style>` stays an `inline-stylesheet` finding: extract it keeping its
page scope and its position among the page's stylesheets. Preserve semantic classes; references use
`page:<manifest-key>` and `asset:<path-relative-to-dist>` tokens.

The planner emits contract schema `h2wp-blocks/2`. It unwraps a single root
app wrapper and `<main>` into `contract.frame`, so page sections are the
children of `<main>`, and turns the source `<header>`/`<footer>` (or manifest
`chrome.*.selector`) into `core/template-part` proposals with their trees in
`contract.parts`; a footer stage 2 recorded as `chrome.trailing` (role
`footer`, e.g. a trailing `<section>`) becomes the footer part too. WordPress
imports every page under its manifest `title` and `slug`: prepare fills a
missing slug from the source file (`blog/x.html` → `blog/x`) and replaces a
missing or shared title (stage 2 can title every page after the site) with the
post's `<h1>` or the page's `<title>`; review both. The compiler renders every page template as
`wrapper(header part, main(post-content), footer part)`, so a page created
later in WordPress inherits the same chrome. Pages must not nest the shared
parts deeper than the top level. `chrome-variant` / `frame-variant` findings
mean pages really differ; active navigation state is ignored when comparing
and `chrome-active-state` reminds you the shared header keeps the first
page's link classes. The planner also maps inline-only text to native
paragraph/heading/list blocks, YouTube/Vimeo iframes to `core/embed`,
`video`/`audio`/`table`/`picture`/`pre` to their core blocks, names top-level
sections through `metadata.name`, and writes literal `:root` color, font and
font-size custom properties to `themeJson.settings` presets.

For blogs the planner also derives the article and listing templates:
it aligns all post pages, keeps identical sections as template content,
turns the title into `core/post-title`, meta values (date, category, reading
time) into bound elements, the prose container into `core/post-content`, and
card lists linking to posts into query loops (`h2wp/related-posts` inside
the article, `inherit:true` in `home`/`archive`). An image that differs per
post (article hero, card photo) becomes that post's `post.featuredImage` and
a `core/image` bound to it (`metadata.bindings` source `h2wp/post-image`); an
article's lead paragraph becomes the post excerpt when the posts have none (a
description every page shares is ignored). A lone card for the newest post
(a listing's lead article) is a one-post query and the grid after it skips
that post (`offset`; the listing then keeps its own query). Post proposals then carry
only the article body. Review these generated templates rather than
authoring them by hand; `article-dynamic-unmapped` marks values it could not
classify.

The planner turns header/footer link groups into menus: a run of two or more
plain page or `#anchor` links (or list items each holding one) with the same
classes becomes `h2wp/navigation` with the source classes (see
`docs/GUTENBERG-CONTRACT.md`); groups with identical links, such as a desktop
nav and its mobile drawer, share one menu, so the owner edits it once in
Site Editor → Navigation. A lone call to action, `mailto:`/`tel:` links and
list items that are not all links stay elements. A container that holds only the links
becomes the menu's list, with its classes and recorded attributes (a toggled
mobile drawer keeps working); lists nested in list items (dropdown submenus)
stay elements. `navigation-static` lists groups kept as elements because
their link classes depend on the link's siblings (`first:`, `last:`...), or
their container spaces children but holds more than the links.
Review it; converting such a group needs matching bridge CSS.

The coordinator alone owns shared theme tokens, styles/scripts, templates,
header/footer parts and menus. Review the generated parts and manifest
fragments recorded in the inventory. Keep `h2wp/navigation {menu}` (or the
source link tree) in the header part; the compiler emits a native
`core/navigation` bound to the imported menu. Never set
`themeJson.settings.spacing.blockGap` to `false`: it emits zero margins over
the source CSS and is rejected. Freeze
shared contract changes before dispatch. Article workers own **article body
blocks only**. The coordinator extracts the article frame, dynamic title,
metadata, related-post queries and shared calls to action into `single` (and
any named article template), optionally through a shared `article-layout` part.
Use native `core/post-title` and exactly one `core/post-content` outside query
loops. Do not hide the article layout or title inside each imported post's
content: a new post must inherit the same frame without copying an old post.
Preserve source coverage when extracting shared sections; use reviewed part
references where the inventory requires them, never fabricated coverage IDs.
Declare a named article template in `themeJson.customTemplates`, for example
`{"name":"article","title":"Article","postTypes":["post"]}`. The default
`single` must also provide the shared layout for posts with no selected template.

Freeze the shared contract before dispatch:

```sh
node assets/scripts/prepare-block-plan.mjs freeze --manifest={workspace}/conversion-manifest.json
```

Freeze invalidates all worker reviews while preserving proposals. Source HTML
changes require a new workspace/plan and reviewed reapplication of mappings.
Never patch hashes manually to approve stale coverage.

## Dispatch at most three workers

Use actual host subagent tools: Codex `collaboration.spawn_agent` or the
corresponding Claude Code agent tool. The CLI writes task state; it does not
launch AI. Read `.gutenberg/tasks.json`; pages group by manifest `family`, then
`kind`, then `page`. The coordinator may split large families into disjoint
tasks, preserving every page exactly once and the frozen contract hash.

Dispatch up to three tasks concurrently. Give each worker its task ID, exact
owned page keys, frozen contract and source inventory. Only its owned page JSON
files are writable. Shared changes are requests to the coordinator. Verify the
representative page first, including editor/visual behavior, then apply the
same mapping to the rest of that family. The coordinator serializes lifecycle
commands to avoid races in the shared state file:

```sh
node assets/scripts/prepare-block-plan.mjs claim --manifest=... --task=family-1 --owner=worker-1
node assets/scripts/prepare-block-plan.mjs complete --manifest=... --task=family-1 --owner=worker-1
```

Workers report resolved finding IDs and concrete implementation/test reasons.
The coordinator records these in `.gutenberg/checkpoint.json` under
`resolutions["<page-key>:<finding-id>"]`. A reason documents evidence, never
permission to drop content. Completion captures output hashes; later changes
invalidate review. Resume failed tasks with the same owner, or explicitly
reassign after confirming the old worker stopped. Never run legacy generators
concurrently because they mutate shared files. Each section ID must occur once
in original order across the page tree. Raw HTML fallback is prohibited.

## Finalize and test locally

```sh
node assets/scripts/prepare-block-plan.mjs finalize --manifest={workspace}/conversion-manifest.json
assets/scripts/convert-remote.sh {workspace} --api=http://127.0.0.1:8080
```

Finalize rejects stale source/contract, incomplete workers, modified completed
outputs, missing/reordered/duplicate coverage and unresolved findings; when
`gutenberg_block_schema.py` ships beside the planner, every violation its
`validate_trees(block-plan/)` reports fails it too (so does a schema module
that cannot answer). Server
schema and block validation provides a separate gate. Upload includes only
the contract and manifest-listed page proposals; local notes/checkpoints and
unrelated block-plan files remain local. Keep existing Astro payload filtering.

The v2 theme uses Gutenberg and its bundled editable blocks. Legacy
HTML/runtime-token rewrites do not apply, and the gates certify the block
editor, not Visual Edit. Visual Edit Lite (latest release,
https://github.com/iOSDevSK/visual-edit-lite/releases; block themes since
1.30) is still recommended to the owner for click-to-edit authoring: install
it on the verification site only after `gutenberg-verify-local.py` passed and
check that it activates cleanly. It is never bundled. Install WooCommerce for
shops. Only the coordinator imports into local WordPress. Verify real block
editor edit/save/reopen, forms, menus, SEO, import resume/idempotence and shop
purchase flows. Compare at 1440/820/390. Parallelize read-only screenshots;
isolate writes and cart sessions. Respect localhost-only user instructions.
For `h2wp-blocks/2` themes `--edit-roundtrip` also runs the new-page gate: a
fresh draft page must render the shared header and footer exactly once, and
packaging refuses a `/2` report without that evidence.

## Benchmark and recovery

Compare the same Gutenberg pipeline with one and three workers in separate
workspaces on identical presentation, blog and shop inputs. The check report
records planning elapsed seconds. Store actual host token usage, repair counts
and gate results in checkpoint `metrics` as `{name,value,unit}` objects. Record
total pipeline wall time externally, including build/compile/import. Target
30% faster at equal quality; do not invent missing measurements or treat a
single-task site as evidence of parallel speedup.

Running prepare again checks existing work without overwriting proposals.
After shared contract changes freeze and re-review tasks. After source changes
start a fresh plan, unless the plan was reviewed and the dist it was prepared
from is still at hand: keep it (`mv {workspace}/astro-project/dist {workspace}/dist.prev`) before the
rebuild, then

```sh
node assets/scripts/prepare-block-plan.mjs refresh --manifest={workspace}/conversion-manifest.json --previous-dist={workspace}/dist.prev
```

carries the review over the edit. It plans both dists afresh and replays the
reviewed edits onto the new plan (base: the previous dist's plan). A page
whose element structure held (tags, classes, attribute names, the recorder's
`data-spa-*` names; text, attribute values and phrasing inside text — bold,
links, line breaks — are content) keeps its review: a text, link or image edit
reopens nothing. A page whose structure changed
gets a fresh proposal and its family task reopens; so does a page whose
reviewed edits reshaped what the source edit changed (the reviewed proposal is
kept in `.gutenberg/displaced/`, `reopenedWhy` says which). A structural change to the
frame or header/footer leaves the contract for review and `freeze`.
Resolutions follow their findings: by id, or to a finding whose items were
all resolved already (an edit dropped some) or were only re-worded; a finding
that gained an item is unresolved again. The JSON summary lists the pages
`refreshed`, `reopened` and `unchanged`, then `finalize` as usual. Never report failed or missing gates as passed. Protected
repo ownership includes the optional localhost SSR adapter
`assets/scripts/gutenberg-prerender-local.py`; it is not copied from legacy R&D.

## Native rebuild and delivery

`assets/scripts/rebuild-theme.sh --manifest=... --api=<localhost-origin>`
recognizes v2, calls the service without legacy judgment flags, and stops after
rebuilding with instructions for verification. It does not manufacture a
passing report or call legacy `make-zip.sh` (which expects clara-content).

Use the native gate and native packager. Set `WS`, `SLUG`, `LOCAL_WP`,
`LOCAL_SOURCE` and `LOCAL_WP_PASSWORD` from the isolated local test environment.
Write `gutenberg-routes.json` as an array of `{ "source": "/index.html",
"target": "/" }` entries covering every imported source page/post/product;
cart and checkout use the functional commerce checks. The gate (a full run,
the default `--scope full`) tests each route at 1440, 820 and 390 pixels.

```sh
python3 assets/scripts/gutenberg-screenshot.py \
  --site="$LOCAL_WP" --theme-dir="$WS/theme/$SLUG" --container="$LOCAL_WP_CONTAINER"
python3 assets/scripts/gutenberg-verify-local.py \
  --site="$LOCAL_WP" --source="$LOCAL_SOURCE" \
  --routes="$WS/gutenberg-routes.json" --user=admin \
  --password="$LOCAL_WP_PASSWORD" --theme-slug="$SLUG" \
  --theme-dir="$WS/theme/$SLUG" --edit-roundtrip \
  --out="$WS/gutenberg-verification.json"
python3 assets/scripts/gutenberg-package.py \
  --theme="$WS/theme/$SLUG" --report="$WS/gutenberg-verification.json" \
  --out="$WS/$SLUG.zip"
```

While repairing, `--scope smoke` is the quick loop: the frontend visual gate
at 1440 only, then the editor canvas at 1440, then the editor's block gate
without the save/reload gates (with `--edit-roundtrip` the database is still
treated as the throwaway fixture, but save/reload, new-post and new-page run
in a full run only). Its report says `"scope": "smoke"`, and
`gutenberg-package.py` and `send-verdicts.sh` refuse it wherever it is put, so
give it its own `--out` in its own directory: the captures go to
`screenshots/` and `editor-screenshots/` beside whichever `--out` is given.
Only a full run is evidence; finish every repair with one. `--workers N`
(default 3) is how many captures the visual and editor visual phases take at
once, one browser each. `--source-dir=<the directory $LOCAL_SOURCE serves>`
keeps the original site's captures in `.h2wp-capture-cache/` beside `--out`
and reuses them on every rerun; an entry is used only while every source
file, the capture code, the Chromium version, the width and the page are
unchanged, and nothing is cached unless `--source` serves exactly that
directory (every route's source page is compared first). Rows say
`sourceCapture: cached|fresh`; the report's `sourceCache` counts them.
Visual rows name their two PNGs (`sourceScreenshot`, `wpScreenshot`), and a
finished visual phase indexes them in `screenshots/captures.json` for
`compare-pages.py --from-captures` (SKILL.md stage 5.5).

The gate fingerprints the generated theme and compares it with the installed
theme through an authenticated WordPress integrity endpoint. Packaging requires
save/reload evidence for every imported page/post/product, registered templates,
and visual differences of at most 1% at all three widths. It rejects incomplete
evidence, stale installed files and changed local files. Any fixes
require rebuilding/re-importing and re-running relevant gates before delivery.
Repair a red gate or an unresolved finding by [repair.md](repair.md).
The report schema is `h2wp-local-verification/2`. Capture the real 1200×900
frontend preview before verification and install the identical screenshot.png.
The gate decodes it, rejects blank images, and binds local and installed SHA-256;
replacing the preview requires new evidence even though it is outside the code
fingerprint. The authenticated import-status endpoint must confirm the current
bundle has finished and every expected page, post, product, menu and media asset
exists. Unresolved pending work or importer errors block packaging.

Editor acceptance compares the actual editable iframe canvas with the public
frontend at actual widths 1440/820/390 and a 900px content viewport height.
The administration window stays desktop-sized while its actual iframe is
resized; this prevents WordPress's mobile administration navigation replacing
the Site Editor canvas. Both viewport dimensions are recorded. Each surface is
loaded in the editor once: its canvas is resized 1440 → 820 → 390 inside an
administration window sized for 1440 (1488×1020), and every frame-level
preparation is applied again at each width. A width that errors on the resized
editor is measured once more on a fresh load (`editorLoad: "resized"` marks a
row measured without a reload). It covers
page/post content, representative
front-page/home/single templates, and header/footer parts. Site Editor templates
must use a real representative post context, never a Content-block placeholder.
On the isolated localhost database, also create a genuinely new post with only
a new title and short body, using the default template. Verify that the shared
article layout, dynamic title, body, header/footer and required article chrome
appear, then save/reopen it and test the named article template if provided.
The test must not duplicate an imported post or copy its wrapper blocks. Record
the new post ID, selected template, screenshots and save/reopen result, then
remove the temporary post. Existing-post screenshots alone do not establish
that future posts inherit a usable template.
WooCommerce renders the shop page through the Product Catalog template
(`archive-product`), never the page's own content, so the gate measures that
template's product grid (`.wp-block-woocommerce-product-template`) in the
Site Editor instead of the shop page's post content; the template's notices,
title, result count and pagination are editor placeholders.
Only editor chrome (including the separate title input) is excluded. Native
scroll captures preserve viewport units and cover the entire canvas; content,
menus and broken editor styling are not masked. Every row must be within 1%.
The admin canvas wrapper may be offset by less than one physical pixel before
painting to match the reference crop's fractional raster origin; this offset is
recorded and leaves internal content-relative layout unchanged. Source-to-frontend comparisons
remain separate. The save/reopen check edits text plus image alt text, direct
menu labels and FAQ answers when those blocks exist in the page, then restores
the original content. Run save/reopen only on the
isolated fixture database; screenshot diagnostics and import-status are read-only.
Frontend checks also decode images and open every native FAQ disclosure, checking
visible nonempty panel content rather than trusting `aria-expanded`. Form/SEO/commerce functional scenarios remain additional
acceptance requirements. The fixture generator `tools/gutenberg-fixture.mjs`
in the protected repository produces presentation/blog/shop workspaces for
these checks; `--compile --http` uses the documented local test service only.

The upload packer normalizes tar/gzip metadata so unchanged workspaces reuse
their saved job. Job state is scoped to the exact API URL; changing servers or
loading old state without that API field opens a new job. An expired or
superseded reused job is recreated once; other refusals retain their normal
error handling and quota policy. `H2WP_STRICT_JOBS=1` (the desktop app)
stops with `JOB_EXPIRED` instead of recreating it.


Inline styles on recorded interactive elements need separate behavioral review.
Extracting `display:none` into a persistent CSS class means removing the inline
style on open no longer reveals a panel. Resolve `runtime-inline-style`
findings with explicit CSS for the recorded open/closed state or native block
behavior; verify visible answer/menu content and link clicks, not merely
`aria-expanded`. Test every trigger, including an initially open accordion and
the last item: an incomplete prerender interaction inventory can omit panels
that are invisible in all resting screenshots.


Shared article templates can use a native `core/query` with
`namespace: "h2wp/related-posts"` and `query.inherit: false` for related articles.
The theme adds the current article to existing exclusions at render time; the
editor applies the same exclusion to its native REST preview without saving a
post ID in the shared template. A standalone template part with no representative
article has no current article to exclude. Keep the native post-template/title/
excerpt/read-more blocks editable. New user-created posts remain eligible for
this query; do not remove them to reproduce an older source screenshot.

A product page's "you may also like" strip becomes WooCommerce upsells. The
compiler reads the links to other product pages outside the description, in
page order and one per product, as `product.upsells` (page keys; planner data
may set its own list, and planner data without one keeps the page's). The
importer stores them as each product's upsells, which the owner edits under
Linked Products. In `single-product` use a `woocommerce/product-collection`
with `collection: "woocommerce/product-collection/upsells"`,
`query.orderBy: "post__in"`, `query.inherit: false` and `perPage` equal to the
source's card count, never the related collection: WooCommerce picks related
products at random, so it cannot reproduce the source's list. Make the strip's
whole section the collection (its `tagName`/`className`, with the heading and
links as inner blocks beside `woocommerce/product-template`): WooCommerce
renders nothing for an empty collection, so a product without upsells shows no
heading over an empty grid.

Source disclosures must remain operable inside Gutenberg as well as on the
frontend. The editor previews semantic `details/summary`, `aria-controls`
panels and recorded `data-spa-toggle` / `data-spa-panel` pairs using transient
state. Opening a panel must not change serialized attributes or its saved
default state. Source scripts and form submissions do not execute in the
editor. Verify opening, closing, editing inside a panel and save/reopen,
including non-FAQ controls; use `tools/gutenberg-disclosure-test.py` on an
isolated localhost fixture for the generic runtime regression.


The visual inventory includes assigned custom templates such as `article`.
Post-body crops are taken inside their real template context, so the editor's
content width matches the published article. If every post uses a custom
template, `--edit-roundtrip` verifies `single` separately by temporarily clearing
one fixture post's template assignment, capturing all three widths sequentially,
and restoring the exact original assignment and content. This never runs in
parallel with other screenshots. The same flag creates a uniquely named fixture
post, checks the normal post editor opens with the template visible, saves and
reopens its body, checks one public title and the saved body (header/footer counts are diagnostic),
and deletes that owned
post in `finally`. These write checks belong only in the disposable test site.

The new-post proof follows WordPress's native template hierarchy
(`single-post-{slug}`, `single-post`, `single`, `singular`, `index`) and compares
the normal editor's resolved template with the generated theme's expected
fallback. It does not assume every site uses `single.html` or semantic wrapper
tags for its header/footer parts.

After moving article content into a shared template, compare the body and
related cards against the source again. WordPress flow-layout rules can reset
source utility margins on direct Post Content children or Post Excerpt wrappers.
Restore measured spacing with scoped source bridge CSS, and use the source's
actual custom-property names for typography colors. Editor/frontend agreement
alone does not prove agreement with the original site.

## Forms: where submissions go (owner steps)

Every imported `h2wp/form` keeps the source's design and ships **Off**: a
visitor who submits is told the form isn't accepting messages yet, nothing is
mailed or stored, and a logged-in owner is told where to switch it on. An empty
submit shows the source's own messages (recorded at stage -1) under the fields.

To connect a form, open the page in the block editor, select the form block and
choose **Submissions** in its settings:

- **Built-in email** — the theme mails each submission to the address in the
  theme's form settings.
- **Contact Form 7** (offered while the plugin is active) — pick the CF7 form
  under **Contact Form 7 form**. Each field lists what it sends as
  ("Automatic (→ your-email)"); change any with its **sends as** menu, or pick
  **Don't send**. A warning names every field the CF7 form requires that no
  field here fills — submissions fail its validation until each has one. CF7
  then runs its own validation, spam checks (Akismet, disallowed words), mail
  and, with Flamingo active, stored messages. Fields are matched by name,
  then by the label written around each tag in the CF7 form ("<label> Your
  email [email* your-email]"). With CF7's reCAPTCHA v3 or Cloudflare
  Turnstile integration set up (Contact → Integration), the form loads it too
  and every submission carries the visitor's token, which CF7 verifies as for
  its own forms (Turnstile shows its widget above the submit button). Known
  limit: quiz and file-upload tags cannot be filled from this form — leave
  them out of the CF7 form you connect.
- **Fluent Forms** (free on wordpress.org; offered while it is active) — pick
  the form under **Fluent Forms form**, map fields the same way. The
  submission goes through Fluent Forms itself: its validation, spam checks,
  captcha, the stored entry (Fluent Forms → Entries), its email
  notifications and its confirmation — a **Same page** message is shown here,
  a **Redirect / Page** confirmation sends the visitor there. A single name
  field fills a Fluent Name field (first word first name, the rest last name).
  A reCAPTCHA (v2 checkbox, v2 invisible or v3), Turnstile or hCaptcha the
  Fluent form contains — or that its global **Autoload captcha** adds — is
  loaded here too and verified by Fluent Forms; a visible check shows above
  the submit button and must be done before sending. Set the captcha keys in
  Fluent Forms → Global Settings (they must be verified there). Known limit:
  file uploads and payment fields cannot be filled from this form.
- **Gravity Forms is not offered** (it is not free, so this theme cannot be
  tested against it). Connect such a site through Contact Form 7 or Fluent
  Forms, or the built-in email.

Errors the plugin reports show under the matching field in the form's own
message style; success shows the source's recorded feedback (toast, inline or
replacement) when there is one, else the plugin's confirmation, else the
block's **Success message**. If the chosen plugin is deactivated or its form
deleted, the form falls back to Off and tells the owner which — no 404 and no
error page. A submission the plugin rejects does not count toward the form's
rate limit, so the visitor can correct it and send again at once. Without
JavaScript the form still posts and the page it returns to shows the same
verdict (a captcha needs JavaScript: without it the plugin refuses the
submission). Hand the owner these steps in the delivery report.
