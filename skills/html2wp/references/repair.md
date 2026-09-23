# Repairing a failed conversion

Read this when a gate is red, a finding blocks `finalize`, or the owner's review
(stage 5.5) found something the gates did not. It covers both targets (HTML theme
and native Gutenberg). The goal does not change with the site: the converted
site looks, moves and works like the original, the block target's blocks are
valid, and a failed conversion is never delivered as if it were fine.

The rules below come from real failures. Site names appear only as examples;
every rule must hold for a site nobody has seen yet.

## 1. Forbidden moves

None of these ever counts as a repair:

- **Loosening a threshold.** Every gate runs at its default `--threshold`; a
  conversion never passes one. The same goes for a `--no-verify` or `--skip-editor` run
  presented as the verdict, or for dropping a route from `gutenberg-routes.json`
  so it is never measured. Route coverage catches only missing posts and the
  blog listing; a dropped page or product route is caught by nothing but you.
- **A fake reason.** A finding resolution
  (`resolutions["<page-key>:<finding-id>"]`) or a report line that says "cosmetic",
  "acceptable", "known issue" or "matches" without a measurement behind it. A
  reason quotes what was measured: the element, the values on both sides, the
  width. See §2.4.
- **Hand-editing the built theme** (`theme/<slug>/`, or the theme installed in
  WordPress) or the Visual Edit plugin. The next rebuild erases a theme edit, and
  a plugin edit forks the product. Every repair goes through the manifest, the
  block plan, a reviewed contract asset, a re-run of a stage, or a converter
  report.
- **Asking the owner to accept a failed gate.** The owner can decide what the
  conversion covers, for example fewer pages. The owner is never asked whether
  a red gate may count as green. A gate that is still red is reported red
  (stage 6.5 sends it as failed) and the delivery report says why.
- **Authoring behaviour or content.** No hand-written "equivalent" JavaScript,
  no invented copy, no dropped section to make a diff go away. A behaviour the
  recorder could not capture is disclosed, never faked.
- **Excusing a moving red.** A diff that changes between runs is a measurement
  problem: rerun it. Only a stable diff tells you something about the
  conversion (§2.3).

## 2. Localize the failure: the same procedure for every pixel gate

A percentage alone tells you nothing. Before you touch anything, find the
element that differs and name the property that differs.

### 2.1 Crop the diff to boxes

Every pixel gate keeps both captures of a failing pair:

| gate | where the pair is |
|---|---|
| -1 | `prerender-parity/` beside `prerender-report.json` (running app vs static capture) |
| A | `verify-static/<file>.<desktop|tablet|mobile>.orig.png` / `.dist.png` (kept only when red) |
| B | `verify-wp/<key>.<desktop|tablet|mobile>.dist.png` / `.wp.png` |
| G-front | `screenshots/<width>-<path>-source.png` / `-wp.png` beside `gutenberg-verification.json` |
| G-editor | the `editorVisual[]` rows of `gutenberg-verification.json` and their captures |
| smoke / parity | `smoke-editor/failure-<step>-*.png` |

Rebuild the mask the gates use: per pixel, the maximum channel difference
greater than 16, on two images padded white to the same size. Label the
connected regions, merge any closer than about 20 px, and list each box's
`x, y, width, height` and its share of the changed pixels. The box holding most
of the diff is where you start. Two shapes are worth recognizing at once:

- **Everything below one y is shifted.** Something above it changed height.
  Take the first changed row, not the biggest box.
- **One tall box the width of the page, both captures of different heights.**
  The pages have different lengths. Compare the heights first (§2.3).

### 2.2 Find the element under the box, on both sides

Open both pages at the same width and the same scroll, at rest. Force the
reveals to their end state first, or the element under the point is an
invisible one. Use the captures' own step, `reveal_all(page)` in
`assets/scripts/lib/capture_ready.py`: it settles every recorded reveal
(`[data-spa-reveal]` → `spa-in spa-done`) and every hook of the design's own.
A design's hook is any class its stylesheets pair with a reveal marker
(`.reveal.in`, `.curtain.in`). Forcing `.reveal` alone leaves a second hook
hidden on one side, which reads as a diff that is not there.

Then, for the centre of each box (page coordinates `cx, cy`):

```js
(([cx, cy]) => {
  scrollTo(0, Math.max(0, cy - innerHeight / 2));
  const el = document.elementFromPoint(cx, cy - scrollY);
  const id = el?.closest('[data-spa-id]')?.getAttribute('data-spa-id');
  const cs = getComputedStyle(el), r = el.getBoundingClientRect();
  const pick = ['display','position','margin','padding','width','height','font-family',
    'font-size','line-height','color','background-color','opacity','transform','gap',
    'grid-template-columns','white-space','object-position'];
  return { tag: el.tagName, cls: el.className?.baseVal ?? el.className, id,
           box: [r.x, r.y + scrollY, r.width, r.height].map(Math.round),
           style: Object.fromEntries(pick.map(p => [p, cs[p]])),
           path: (() => { const p = []; for (let n = el; n && n !== document.body; n = n.parentElement)
             p.unshift(n.tagName.toLowerCase() + (n.id ? '#' + n.id : '')); return p.join('>'); })() };
})
```

`data-spa-id` is the recorder's stamp, and it survives into WordPress, so it
pairs the same element across the two sides even after the markup has been
rewritten into blocks. On an image the Gutenberg planner drops it unless a
recorded interaction targets that image, so pair images by `src` file name and
`alt`. Without a stamp, pair the elements by text content and tag. Then walk up the ancestors on both sides until the boxes agree: the
first ancestor whose box or style differs is the cause. The element that
merely moved is only the symptom.

### 2.3 Classify what you found

| what differs | usual cause | where to look |
|---|---|---|
| a class missing in WP (`.in`, `.is-visible`, `spa-in`), opacity 0 / translateY left | a source script that runs in the original never runs in WordPress | §6, the reveal incidents |
| margin / padding / gap differs, same classes | cascade: a WordPress layout rule or a reset outranks the source rule (layers, `revert-layer`, flow-layout margins, `blockGap`) | the WordPress rule that wins: DevTools "computed → rule" on the WP side |
| font-family / colour differs | a stylesheet missing or out of cascade order on that page (a per-page sheet, an inline `<style>` between links) | the page's `<head>` order on both sides |
| an image blank on one side | not decoded (lazy image outside the viewport) or a request failed | the gate's `failedRequests` / `behavior.images` |
| only the page height differs | a shell sized for the chrome (`min-h-screen` wrapper), `wpautop` breaks, a lost section | SKILL.md gotchas, and read the side-by-side |
| the diff moves between runs | a race: a decode, a late reveal, a network request | §2.4 |

### 2.4 Is it stable? Measure it twice

Pixel gates already capture a failing pair twice and keep the second
measurement. A dropped offscreen raster picks a different side each run, and a
real difference does not. Do the same by hand before you decide anything:

1. Screenshot the ELEMENT (the box from 2.1), not the full page. It is
   rasterized in the viewport, so it is immune to dropped decodes.
2. Capture the ORIGINAL against ITSELF, twice, at the same width and band. If
   original-vs-original differs at that box by as much as original-vs-converted,
   the difference is the source's own motion or loading race, not the
   conversion.

That second measurement is what makes a reason TRUE. Write it with both
numbers, the element and the width, e.g. *"`.about figure` @1440: source vs
source 1.61%, source vs WP 1.61%; the last card is mid-fade on one capture of
the original itself."* A resolution, or a line in the conversion report, that
cannot quote such a pair is a fake reason. Such a reason explains a red. It
never turns it green: the gate is rerun until the capture is at rest, and
what the gate says is what is reported.

### 2.5 Motion and behaviour: compare reveal timing

Pixel gates compare AT REST, so they cannot see whether a reveal, a stagger or
a scroll-driven swap happens when and how the original's does. This release
has no behaviour gate (a proper one is planned for the next). After gate B/C
(HTML) or G-front (Gutenberg), compare the reveal timing yourself, between
the original and the conversion:

- **What the original does is recorded.** Stage -1 (`prerender-spa.py`) stamps
  each reveal with `data-spa-reveal-at`: the depth into the viewport it waits
  for, `t<ms>` for one on a timer, `+<ms>` for its own delay and `+i<step>` for
  a stagger by position. The runtime (`spa-runtime.js`) replays exactly those
  values on both targets, and the stamps survive into WordPress. A reveal's
  duration is measured on the live page, and a route whose first frames came
  late under load measures it short; an element found on several routes (the
  shared header) therefore carries one duration on all of them, the one most
  routes measured (`revealDurations` in the prerender report lists each).
- **Check the replay against the original.** Open the original and the
  converted page at the same width with animations on and
  `document.documentElement.style.scrollBehavior = 'auto'`. Scroll both in
  small steps (about 50 px) and note, per reveal, the scroll position at which
  it reaches its revealed state (`spa-in`, the design's `.in`, or computed
  opacity 1). Note the load-time reveals and each card's delay in a listing
  too. Compare the two lists.
- **A deterministic mismatch must be fixed.** The original does the same thing
  on every load and WordPress does something else: a reveal that never plays,
  plays at a different depth, loses its stagger or starts hidden in the
  editor. Localize it as in §2 and use a lever from §3. When the recorded
  value itself is wrong, re-record (stage -1). When the replay is wrong,
  report it (§5).
- **A live race is measured, never assumed.** When the original is a live site,
  load it cold at least 10 times. If the live site itself splits, the
  conversion takes the majority branch. Write the measured split into the
  conversion report. Example: cards settled about 1 px from the observer's
  trigger, and live showed them at load on 6 of 10 cold loads and on scroll on
  4 of 10 (a font/layout race). A difference the live site reproduces on every
  load is never a race.
- **Every scroll-through must scroll instantly.** On a page with
  `scroll-behavior: smooth`, each `scrollTo` becomes an animation that the next
  step retargets. The walk creeps a few hundred pixels down a long page, and a
  reveal near its depth is photographed revealed on one run and at its
  starting offset on the next. The gates now walk instantly and give the page
  its own scroll behaviour back. Any scroll you script by hand must do the
  same.

## 3. The levers you may use

Everything the coordinator may change. Anything outside this list is either
forbidden (§1) or a converter gap (§5).

**Input and stages**
- Re-record the app when the capture is wrong: `prerender-spa.py` again, with
  `--routes=` for a route its link crawl never reached (a family no page
  links to). `--gates-only` only re-verifies an existing capture.
- Re-run a stage with its documented options: `prerender-spa.py --gates-only`,
  `optimize-images.py --input <dir> --remote --apply` (then every later
  stage), `stage2-gates.sh {workspace} --original-remote={workspace}/optimize-images-report.json`,
  `gate-a-bisect.sh` to find the step, `normalize-form-fields.py --apply`
  (stage 2.65, then the last rebuild).
- After ANY re-capture or input change, rebuild from stage 1 with
  `astro-project/` deleted first (a stale `public/` masks the fresh capture).

**Manifest (`conversion-manifest.json`)**
- `pages[].kind`, `pages[].chrome`, `chrome.*.selector`, `chrome.frontOwnsFooter`,
  `blog.articleMain` / `articleBody` / `articleCategory` / `articleNav`,
  `shop.*` hints and `shop.products[]`, `nav[]`, `declaredCollections`,
  `utilityPages`, `site.*`. See `assets/MANIFEST.md`. A manifest edit is a
  digest-matched re-run (`rebuild-theme.sh`).

**Gutenberg block plan (`block-plan/`)**
- Page proposals (`block-plan/pages/<key>.json`, the owning worker's file):
  block choice, `className`, `h2wp/field` `name`s, a page's own `styles[]`
  (at most 20, source order) and its own scripts.
- `contract.scripts[]` (dist `.js`/`.mjs`) and `contract.headScripts[]`: only
  **reviewed source scripts**. The planner extracts every inline source script
  to `assets/gutenberg-script-<hash>.js` and names that file in the
  `source-runtime` finding. You read it and list it only when it is a
  self-contained, DOM-only behaviour (a reveal observer, a drawer toggle): no
  framework hydration, no network writes, no reading of markup WordPress
  does not render. The prerender's own `spa-runtime.js` is listed
  automatically.
- `contract.editorStyles[]` (dist `.css`, canvas only; compiled into
  `config.editorStyles`): a small coordinator-authored sheet holding the
  **settled state** a page's reviewed scripts reach on the frontend, because the
  editor runs no source script. It must never style a box the runtime owns
  (the compiler warns, §6 *stale CSS*), and the frontend never loads it.
- `resolutions["<page-key>:<finding-id>"]` in `.gutenberg/checkpoint.json`: at
  least 12 characters, one per finding. The id is in `.gutenberg/inventory.json`
  (a digest of code, section and detail, so it changes when the finding
  does). A code a page raises several times is ONE finding whose `items` list
  every occurrence (`{section, detail}`, its id a digest of the code and all
  of them): read every item, and let the one reason answer for each. The
  reason states what you did and what you measured.
- `prepare-block-plan.mjs refresh --previous-dist=…` after a source edit
  carries a reviewed plan over; `freeze` after any shared contract change.

**Verification environment**
- `test-env.sh reset` (or `down` + `up`) before every install, `clone` for the
  smoke test, and a fresh `up` for the certifying install.

## 4. Gate by gate

Each entry: what the failure looks like → how to localize it → the likely
causes → your lever → when to stop and report (§5).

Green pixel gates are not the end: after gate B/C (HTML) or G-front
(Gutenberg), compare the reveal timing (§2.5). It is mandatory on both targets.

### Gate -1 — running app vs static capture (`prerender-spa.py`)

- **Looks like:** `FAIL <key> @<width>: N% differs from the running app`;
  `prerender-report.json` → `pages[key].parity[width]`.
- **Localize:** §2 on the `prerender-parity/` pair. The live app is the
  original.
- **Causes:** a mid-animation capture (`scale(`/`translate(` left in a
  `style=`); a reveal the capture played and the app had not yet (or the other
  way round); a gallery swap replayed at load; a basket the recorder filled;
  web fonts or images still loading on the live side.
- **Lever:** `--gates-only` rerun. When the live app races itself, measure the
  split over at least 10 cold loads (§2.5) and record it in the conversion
  report. The gate's own verdict still stands.
- **Stop:** the static capture is wrong the same way on every run (a recorded
  state, a lost element). That is the recorder's defect, so report it (§5).
  Never pass `--no-verify`.

### Gate -1b — recorded behaviour replays on the static page

- **Looks like:** `pages[key].behavior` rows with a trigger that did not open
  its panel, a swap that did not swap, an empty-submit message that did not show.
- **Localize:** click the trigger yourself on the static page, then read its
  `data-spa-*` attributes and the panel's computed `display`/`visibility`.
- **Causes:** a state-changing control recorded as a disclosure; a panel open
  at rest recorded the wrong way round; a quantity stepper that counts.
- **Lever:** re-run the stage after the fix ships. You have none of your own.
- **Stop:** always a recorder or runtime defect → report (§5).

### Gate A — source vs build (`verify-static.py`)

- **Looks like:** `GATE A FAILED`; `verify-static/report.json` →
  `pages[file][desktop|tablet|mobile].diffRatio`, `consoleErrors`,
  `inheritedConsoleErrors`, `failedRequests`, `missingFromDist`, `links`.
  A page marked `identicalByHash` (widths `status: identical-by-hash`) was
  not photographed: its bytes and every file it loads (`sha256`) are the same
  on both sides, so it cannot fail the raster; `identity.disabled` says why a
  run proved nothing that way.
- **Localize:** `gate-a-bisect.sh` names the step first (U → working copy →
  dist-s1 → dist-s26 → dist), then §2 on that step's pair.
- **Causes:** stage 0.5/0.6 cost (re-encode, sizes), a stage 1 build defect,
  2.6/2.65 edits; hotlinked images that the gate refuses to fetch on the
  original; the network.
- **Lever:** revert 0.5/0.6 for this site or raise the WebP quality (they are
  optional); `--original-remote=…` whenever 0.5 ran with `--remote`; a
  manifest fix and a rebuild.
- **Network flakes on the original:** when the ORIGINAL side failed to load a
  resource (console or network errors on the source capture, e.g. an image
  host answering `net::ERR_FAILED`), the measurement is void. Rerun it. Never
  accept it, and never patch the conversion for it. That is different from an
  asset the source is missing on every run (a dead `<link>`, a 404 each
  time): that is `inheritedConsoleErrors`. Disclose it in the report and do
  not "fix" it, because deleting the source's dead `<link>` is editing the
  client's site.
- **Stop:** red at bisect step 2 (stage 1) with a correct manifest → report.

### Gate A2 — structure (`verify-parity.mjs`)

- **Looks like:** a region (`header`/`nav`/`footer`/`main`) with a lost
  attribute, a changed link target, an uncompared region naming the manifest
  field that fixes it.
- **Lever:** the named manifest field. Stage 2.6/2.65 reports must survive a
  re-run, because A2 reverses them from those reports.
- **Stop:** a transformation that is neither of the two A2 normalizes, with a
  correct manifest → report.

### Gate B — build vs WordPress (HTML target, `verify-wp.py`)

- **Looks like:** `report.json` → `pages[file][width].diffRatio`,
  `failedRequests` (B3), `checks.*` (B2 style assertions, `routing`, `llms`,
  `front`, `storedSources`).
- **Localize:** §2. Uniform vertical offsets on every page with a shared header
  come from cascade layers or `blockGap`. A front page near 99% while every
  subpage is at 0% is an unrewritten asset path in its own head.
- **Causes:** see the SKILL.md gotchas. Most are the generator's.
- **Lever:** manifest (chrome, regions, `frontOwnsFooter`), `rebuild-theme.sh`.
  Always pass `--wp-cli`, or C2b reports NOT RUN.
- **Stop:** uniform offsets, a front-page asset path, a token printing as text
  → report.

### Gate C — WordPress behaviour (HTML target)

C1 SEO, C2/C2b/C2c the editor preview and the stored sources, C3 blog
fidelity, C4 menus, C5 collections, C6 shop.
- **Looks like:** `checks` entries with `ok:false`, or NOT RUN. A NOT RUN is
  never a pass.
- **Causes:** C3 posts without images (hotlinked images never fetched,
  §6), a byline or date baked in from one article; C4 a nav selector
  matching twice; C5 a group the editor refuses.
- **Lever:** `blog.*` / `nav[]` / `declaredCollections` in the manifest,
  `--remote` at stage 0.5.
- **Stop:** a congruence rule that refuses a group a person can see is a list,
  or a derivation that cannot place an ordinary field → report.

### smoke-editor (HTML target, `smoke-editor.py`)

- **Looks like:** `smoke-editor/report.json` → `steps.<step>` with a failure
  dump (screenshot, iframe URL, console errors, `data-cve-path` count).
- **Causes:** `editPreviewParity` shot a page mid-reveal or before its hero
  painted; `mediaReachable` blocked by a tinting overlay; a variant part's
  preview page that became a post.
- **Lever:** run it on the clone. A parity pair is recaptured once the reveals
  are at rest (`reveal_all`, `media_ready`). Rerun before you judge.
- **Stop:** a failure that repeats on a fresh clone → theme contract or
  generator → report. Never edit the plugin.

### woo-coverage (both targets, `audit-woo-coverage.py`)

- **Looks like:** `GAP  …` lines; the exit code is the number of GAPs;
  `woo-coverage/report.json`.
- **Causes:** a product page without a buyable form; a cart or checkout that
  cannot ship on a test site (add a free-shipping rate on the test environment
  only); no product on sale, so the sale presentation went untested; clean-up
  left behind.
- **Lever:** fix the test environment (shipping rate, a product on sale) or the
  manifest's `shop.*`, then rebuild and rerun.
- **Stop:** a GAP on a correctly described shop is the generator's → report.

### G-front — source vs WordPress frontend (Gutenberg, `gutenberg-verify-local.py`)

- **Looks like:** `visual[]` rows `{path, width, diff, behavior, passed}` with
  `diff > 0.01`, or `behavior.passed:false` (an image not decoded, a
  disclosure that does not open, a menu whose links do not become actionable).
- **Localize:** §2 on the `screenshots/` pair. The capture scrolls through in
  700 px steps, disables animation and then compares at rest.
- **Causes:** a source script that never runs (the reveal incident below); a
  header variant rendered with the shared part; a per-page sheet or inline
  style out of cascade order; a figure or post-content wrapper that takes a
  box the source never had; a date or order on a listing.
- **Lever:** the block plan (proposals, `contract.scripts`, page `styles`),
  the manifest, `rebuild-theme.sh`, re-import.
- **Stop:** a runtime or compiler box (a `wp-`/`h2wp-` wrapper) differing
  from the source with a correct plan → report.

### G-editor — the editor canvas vs the frontend (`gutenberg-editor-visual.py`)

- **Looks like:** `editor[]` rows with `invalid`/`unknown` blocks or
  `unresolvedTokens`; `editorVisual[]` rows over 1%; errors such as
  `Canvas height changed while capturing` or `cannot be scrolled into view`.
- **Localize:** §2 on the canvas iframe against the frontend page. Every row
  must be within 1%.
- **Causes:** invalid blocks (a proposal the compiler accepts but the editor
  re-serializes differently); content a page script reveals, which the editor
  shows in its hidden state; per-page sheets outside the source layer; canvas
  width against the content column; a box-less region (`display:contents`)
  measured by the union of its descendants.
- **Lever:** `contract.editorStyles` for a settled script state; the proposal
  for invalid blocks.
- **Stop:** layering, width, height instability, the drop zone → the canvas
  runtime's to fix → report (§6).

### G-roundtrip — save / reopen, new post, new page (`--edit-roundtrip`)

- **Looks like:** `editor[].roundtrip` with `invalid`/`unknown` or
  `textPersisted:false`; `newPost` / `newPage` with `passed:false` (header or
  footer not exactly once, the article frame missing, the default template
  wrong).
- **Causes:** a template that holds content instead of `core/post-content`;
  the article layout inside each post instead of `single`; shared parts nested
  below the top level.
- **Lever:** the templates and parts in the contract (the coordinator owns
  them), then `freeze` and re-review.
- **Stop:** a correct template that the new-page proof still rejects → report.

### G-import — import state and preview

- **Looks like:** `import.passed:false` (pending work, importer errors, missing
  pages, posts, products, menus or media), `preview.passed:false` (the
  installed `screenshot.png` differs from the local one).
- **Causes:** a slug conflict (WordPress suffixes numeric slugs: `/404/` lives
  at `/404-2/`, and the live path is the importer's record); an install into a
  WordPress that is not clean; a store page created twice.
- **Lever:** `test-env.sh reset`, reinstall, rerun. Re-shoot the preview and
  reinstall the identical file.
- **Stop:** a repeatable importer error on a clean install → report.

### Route coverage (`routeCoverage`)

- **Looks like:** `routeCoverage.missing` lists the posts and the blog listing
  absent from `gutenberg-routes.json`.
- **Lever:** add every one. The routes file must cover every imported page,
  post and product. Cart and checkout are covered by woo-coverage instead.
  Leaving a route out is never a fix.

### Finalize findings (`prepare-block-plan.mjs finalize`)

- **Looks like:** `<key>: unresolved <code> [<id>]`, stale or mismatched
  worker output, coverage that is incomplete, duplicated or reordered, raw HTML
  blocks.
- **Lever:** fix the proposal, or resolve the finding with a TRUE reason. A
  worked example, `query-card-title`: *"card 3 prints 'Winter light, part 2';
  its post title is 'Winter Light II'. The card now shows the post title, and
  the difference is listed in the conversion report for the owner."* Quote
  both strings. A reason like "headline differs, fine" is fake.
- **Stop:** a finding that asks for something the block vocabulary cannot
  express → report. Raw HTML fallback is prohibited.

## 5. Stop and report a converter gap (Tier B)

Ask one question: **would the next site hit this too?** If yes, and the site is
described correctly, the defect is the converter's (the planner, the
compiler, the runtime, the recorder or a gate), not this site's. Patching
around it with site CSS or a resolution hides it from every later site. A
coordinator who CSS-patches a canvas-height bug hides a converter defect from
the next site.

Before you report, rule out your own stale state: a stale local
`astro-project/`, stale stored sources or an old plugin build look exactly like
a converter bug. Put the skill's `VERSION` and the service's version (its
`/health` answer) in the report, so an incident in §6 that is already fixed can
be told apart from a recurrence.

Report through `/v1/report` with: the gate and its report row, the page key and
width, the element (tag, classes, `data-spa-id`), the computed values on both
sides, the manifest excerpt and plan fragment, and what you expected against
what rendered. Deliver nothing as passed in the meantime: send the verdicts as
measured (stage 6.5), with `--outcome=abandoned` when you stop.

## 6. Incidents → rules

**Kind** says who acts. *coordinator*: you pull the lever. *fixed in the
converter*: recognize the symptom, rule out stale state, and report (§5) if it
still happens. Do not work around it. The hashes name the converter commits,
for the report's reader.

| incident (gate) | rule | lever | kind |
|---|---|---|---|
| A transparent header over the front page's hero and a solid one elsewhere: the shared header was the first page's, so inner pages got white links on white (G-front) | A header that differs by more than its current link is a variant. Each variant used by front or ordinary pages gets its own part and template. A current link is recognized by class alone | none if planned right; read `chrome-variant` findings; check which part each page's template renders | fixed in the converter (b6d88f8) |
| A shop converted onto WooCommerce's plain fallback catalog and product templates (G-front, woo-coverage) | `archive-product` and `single-product` are derived from the shop's own pages and `shop.*` hints, never from class names. Product pages that differ outside those regions raise `product-template-variant` | `shop.*` hints in the manifest | fixed in the converter (30b405f) |
| A component form's email input with neither `name` nor `id` (finalize `field-name`; HTML: an empty submission) | A field the browser does not name is never submitted. The form's only unnamed email/tel/url field is named by its type. Anything ambiguous stays a finding | HTML: stage 2.65 `normalize-form-fields.py --apply`. Gutenberg: set the `h2wp/field` `name` in the proposal (what a mail handler expects: `email`, `phone`, `message`…) and resolve with the name chosen | coordinator (auto-naming: df6f4df) |
| A source's inline reveal script never runs in WordPress, so `.reveal` never gets `.in` and sections stay hidden (G-front, e.g. clara `/index_v5/` 1.4%) | A behaviour the source's OWN script performs does not happen unless that script is shipped. The planner extracts it and blocks on `source-runtime`. Review the file and list it; then the editor, which runs no script, needs the settled state as canvas CSS | `contract.scripts` += the reviewed `assets/gutenberg-script-<hash>.js`; `contract.editorStyles` += a sheet setting the revealed end state (e.g. `.reveal{opacity:1;transform:none}`); resolve `source-runtime` naming the file and what it does | coordinator |
| Reveal margins guessed after the first 16 components, so a late component waited as deep as another it resembled (G-front, a scroll step late) | Every component's reveal margin is measured. One with no margin reveals on entry | re-run stage -1 once the build carries it | fixed in the converter (e1fd04a) |
| A fast scroll, an anchor jump or a 700 px capture step carried a reveal past the viewport between two frames, and it never played (B/G-front: an image 40 px low on one run in two) | A reveal the page has scrolled past, or one on screen at the end of the document, plays | none; a reveal stuck at its start offset on a current build → report | fixed in the converter (7da8bd5) |
| Smooth scrolling (`scroll-behavior: smooth`) turned a gate's 700 px scroll-through into animations the next step retargeted: an image near its reveal depth photographed revealed on one run and 40 px low on the next (gate B) | Every scroll-through sets `scroll-behavior: auto` for its walk and restores it after | none in the gates; do the same in any scroll you script | fixed in the converter (2aa098c) |
| A listing's stagger lost inside WordPress's `<li>` wrappers (every card index 0) | Stagger is counted at the first level whose siblings share the trigger | none | fixed in the converter (f1ecbc4) |
| The live original races itself: cards settle about 1 px from the observer's trigger and show at load on 6 of 10 cold loads, on scroll on 4 of 10 | Measure the live site over at least 10 cold loads (§2.5). The conversion takes the majority branch, and the report states the split. A diff the live site reproduces deterministically is fixed, never accepted | the report line with the measured split | coordinator |
| A late card mid-fade in one capture of the ORIGINAL (smoke parity 1.6%) | A capture race is measured source-vs-source (§2.4). The gate is rerun with the page at rest (`reveal_all`). A gate's red verdict is never flipped by the reason | rerun | coordinator (method: 6338b34) |
| Per-page source sheets reached the editor unlayered while the shared source sat in a layer, so `revert-layer` skipped them (`/links/` 0.2% → 7-9%) | In the editor, every source sheet (shared and per-page) sits in the one source layer. The canvas body takes its background from the source's layers | none | fixed in the converter (209b385) |
| A post-only canvas ran the full width against an 832 px content column (G-editor) | The canvas keeps the frame `<main>`'s content column, re-measured on resize | none | fixed in the converter (3d8f5c6) |
| A `display:contents` post-content measured as the union of its descendants included a `position:fixed` off-canvas drawer and the editor's drop zone: `Canvas height changed while capturing`, then 20-32% | A fixed box and editor chrome are not part of a region. Report a canvas-height error on a current build; never "wait longer" or hide the drawer | none | fixed in the converter (6ad308b) |
| Stale files after a re-convert: the new theme was unpacked over the old theme directory, so an old content-hashed `front-<hash>.css` stayed beside the new one and the ZIP carried an extra file | Every re-convert starts from an EMPTY theme directory: the earlier one is moved aside, and restored only if the conversion fails. Compare the ZIP listing with the files the plan expects | an empty `theme/` before unpacking; the ZIP listing check | whoever unpacks: the coordinator, or the app runtime (fixed there) |
| Stale CSS after a re-convert: a reviewed contract, editor or per-page sheet written against older compiler output still styles a box the runtime now owns (`figure.h2wp-source-image{display:block}`, 1-5% on every page with a photo) | After every re-convert, read the `theme-report.json` warnings that name a sheet, a selector and the runtime file owning the class. Delete the rule unless the design needs something the theme does not give | remove the rule from your reviewed sheet, rebuild | coordinator (warning: ae417ae) |
| Stale state around a re-convert: a new capture masked by an old `astro-project/public/`; stored sources, media files and Site Editor `wp_template_part` rows surviving a re-import | Rebuild from stage 1 with `astro-project/` deleted. Install into a reset or fresh WordPress only | `rm -rf astro-project/`; `test-env.sh reset` or `down` + `up` | coordinator |
| Hotlinked images (Lovable/v0 exports load from `images.unsplash.com`): posts with no hero, listing cards as empty frames, gate C3/route coverage red | A picture WordPress cannot attach does not exist for it. Localize hotlinked rasters before stage 1, and let gate A's original load exactly those URLs | `optimize-images.py --remote --apply`, then `stage2-gates.sh --original-remote=…` | coordinator |
| The image host answered `net::ERR_FAILED` for the untouched original (gate A, `blog.html` desktop 1.5%) | When the original side fails to load resources, the measurement is void. Rerun it; never accept it, never patch the conversion for it. Only an asset missing on every run is inherited and disclosed | rerun; the report's inherited-errors list | coordinator |
| Articles 404 after import (stale rewrite rules; plain permalinks serve the front page for every path) | The theme heals stale rules once per structure. A 404 on a current build → check the permalink structure and `.htaccess`, then report | none | fixed in the converter (a62c8f6, b226b23) |
| Weekday-first dates (`Thursday, Feb 15, 2024`) bound as categories | Dates with the weekday first parse as dates | none | fixed in the converter (3d83352) |
| A hand-ordered listing (printed dates not newest first) re-sorted by date | Posts keep their listing position as `menu_order`, and listings order by it (`contract.postsOrder: "listing"`) | check the plan says `listing` when the dates are not newest first | fixed in the converter (424fbf3) |
| One card's headline differs from its post's title | Bind the title, show the post title on every card, and report the odd card (`query-card-title`), never freeze one headline on all cards | resolve quoting both strings (§4 finalize) and list it for the owner | coordinator |
| WooCommerce install in a preview: the install step was not WordPress's file owner, so `wp plugin install woocommerce` failed ("Could not create directory wp-content/upgrade"); `wp option update` on an onboarding option exited 1 although the value took (WooCommerce filters `woocommerce_task_list_hidden`) | The preview or test environment's owner installs WooCommerce (`test-env.sh up`, or the app), never the coordinator by hand. When wp-cli cannot write, download the pinned WooCommerce ZIP and upload it through the admin plugin screen, as the editor plugin is installed. Judge an option by reading its value back, never by the exit code | the environment's own install step; read options back | environment owner |
| A retry created a second draft Shop page, or stopped at "URL conflict for /cart/" | The importer ADOPTS WooCommerce's shop, cart and checkout pages and never creates them. Only the coordinator imports | a clean environment; `reset` refuses after a Woo-installing `up`, so use `down` + `up` | fixed in the converter (ce81038) |

## 7. Before you call it done

- Every gate that ran is green on a certifying run, or reported red with its
  measured reason and a report filed (§5).
- Every finding is resolved with a reason that quotes a measurement.
- Every person-photo box, reveal and disclosure was checked at rest AND with
  motion on, and the reveal timing matches the original with no deterministic
  mismatch (§2.5).
- The side-by-side of every page was read (stage 5.5), because the gates
  excuse listings, posts, shop pages and cart/checkout from pixel comparison.
- Stage 6.5 sent what the gates said, not what you hoped.
