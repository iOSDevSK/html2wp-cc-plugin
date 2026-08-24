#!/usr/bin/env node
/**
 * Chrome variant grouping — the ONE implementation both stage 2.5
 * (capture-chrome.py, via the JSON this writes) and stage 3 (make-theme.mjs,
 * via direct import) consume. Two implementations would disagree about what
 * "the same chrome" means, and then the at-rest captures would be taken per
 * one partition and applied per another.
 *
 *   node chrome-groups.mjs --manifest=conversion-manifest.json
 *     → writes {workspace}/chrome-groups.json
 *
 * A "group" is a set of pages whose chrome is the same DESIGN. Pages get
 * there by settling two kinds of noise the raw export bakes in:
 *
 *   - active-nav state: every page marks its own nav link as current, which
 *     would make a per-page header unique per page (7 "variants" of one
 *     header). Each link settles to its page-weighted majority class value.
 *   - runtime state: classes the site's own JS toggles (sticky headers,
 *     open drawers) are scroll/interaction state frozen at export time, not
 *     authored structure. Read from the site's own scripts, never guessed.
 *   - path depth: chrome copied from a nested page writes "../index.html"
 *     where a top-level page writes "index.html" — same target, same chrome.
 *
 * What still differs after settling is REAL design variance the source
 * authored — and since the owner requires every page 1:1, each group ships
 * as its own template part rather than being flattened to a winner. That
 * decision (emit all groups) lives in make-theme.mjs; this module only
 * decides the partition.
 */

import { readFileSync, writeFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { entryKind, safePathUnderRoot } from './lib/safe-path.mjs';

// A tag's attribute run may legitimately contain '>' inside a quoted value
// (Tailwind: class="[&>svg]:size-3").
const TAG_ATTRS = `(?:[^>"']|"[^"]*"|'[^']*')*`;

function classOf(tagHtml) {
  const d = tagHtml.match(/\bclass="([^"]*)"/i);
  if (d) return { quote: '"', value: d[1] };
  const q = tagHtml.match(/\bclass='([^']*)'/i);
  if (q) return { quote: "'", value: q[1] };
  return null;
}

/**
 * A chrome link's target as a SITE-ROOTED path, resolved against the page the
 * fragment was taken from.
 *
 * Stripping leading "../" is not the same thing and breaks on every
 * directory-routed export (Next.js / Astro / Hugo / Gatsby — the input class
 * this converter advertises). There a page's link TO ITSELF is written
 * href="index.html" from inside its own folder, while every other page writes
 * href="../about/index.html". Folded by prefix-strip those are "index.html"
 * and "about/index.html": the same target under two keys. Consequences,
 * both observed on dexler: the group key differs on every page that is itself
 * a nav target, so one header design split into a singleton group per such
 * page; and the active/resting vote never sees the highlighted instance beside
 * its resting ones, because they sit in different href buckets, so the pair it
 * reports is assembled out of whatever is left.
 */
export function rootHref(href, pageFile) {
  const raw = String(href || '');
  if (!raw || raw.startsWith('#') || raw.startsWith('//') || /^[a-z][a-z0-9+.-]*:/i.test(raw)) return raw;
  const cut = raw.search(/[?#]/);
  const path = cut === -1 ? raw : raw.slice(0, cut);
  const suffix = cut === -1 ? '' : raw.slice(cut);
  const base = path.startsWith('/') ? [] : String(pageFile || '').split('/').slice(0, -1);
  const out = [];
  for (const seg of [...base, ...path.split('/')]) {
    if (seg === '' || seg === '.') continue;
    if (seg === '..') { out.pop(); continue; }
    out.push(seg);
  }
  return out.join('/') + (path.endsWith('/') && out.length ? '/' : '') + suffix;
}

const HREF_SRC_RE = /\b(href|src)=(["'])([^"']*)\2/gi;
const rootTargets = (html, pageFile) =>
  html.replace(HREF_SRC_RE, (m, attr, q, val) => `${attr}=${q}${rootHref(val, pageFile)}${q}`);

/**
 * Classes the site's OWN JavaScript toggles — runtime state, not design.
 *
 * Minus the ones it also READS with `classList.contains(...)`. That call is
 * the script asking the MARKUP a question, so the markup is the source of
 * truth for it and its per-page value is authored design, however much the
 * script moves it afterwards. The shape is ordinary: a header drawn over a
 * hero is authored `class="nav over"`, the script reads that once
 * (`overHero = nav.classList.contains('over')`) to choose its scroll
 * threshold, and then removes and re-adds `over` as the visitor scrolls — so
 * a scan for add/remove/toggle alone calls it state and strips it. Measured
 * on a 32-page photography site: 10 pages authored the transparent header
 * and 22 the opaque one, and stripping `over` merged all 32 into ONE design
 * group, which ships one of those two headers to every page — the
 * 11–17%-of-pixels flattening this whole partition exists to prevent. A page
 * that never authored the class never receives it, because the script's own
 * branch is gated on having read it.
 *
 * The error is asymmetric, which is why the exemption is worth its risk: an
 * extra group costs one more template part, each captured at rest and each
 * correct, while a missing group ships the wrong design to a whole section.
 */
export function volatileClasses(inputDir) {
  const toggled = new Set();
  const read = new Set();
  const scan = (dir) => {
    for (const e of readdirSync(dir)) {
      const p = join(dir, e);
      // lstat-based: a symlink here would have this reading JS from wherever
      // it points — see lib/safe-path.mjs.
      const kind = entryKind(p);
      if (kind === 'dir') { if (!['node_modules', '.git'].includes(e)) scan(p); continue; }
      if (kind !== 'file') continue;
      if (!/\.m?js$/i.test(e)) continue;
      const js = readFileSync(p, 'utf8');
      for (const m of js.matchAll(/classList\.(?:toggle|add|remove)\(\s*["'`]([^"'`]+)["'`]/g)) {
        for (const c of m[1].split(/\s+/)) if (c) toggled.add(c);
      }
      for (const m of js.matchAll(/classList\.contains\(\s*["'`]([^"'`]+)["'`]/g)) {
        for (const c of m[1].split(/\s+/)) if (c) read.add(c);
      }
    }
  };
  try { scan(inputDir); } catch { /* no input dir to scan */ }
  for (const c of read) toggled.delete(c);
  return toggled;
}

export function stripVolatile(html, volatile) {
  if (!volatile.size) return html;
  return html.replace(/\bclass=("([^"]*)"|'([^']*)')/gi, (m, _q, dbl, sgl) => {
    const q = dbl === undefined ? "'" : '"';
    const v = dbl === undefined ? sgl : dbl;
    const all = v.split(/\s+/).filter(Boolean);
    const kept = all.filter((c) => !volatile.has(c));
    return kept.length === all.length ? m : `class=${q}${kept.join(' ')}${q}`;
  });
}

/**
 * Settle each nav link to its page-weighted majority class value WITHIN THE
 * PROVIDED LIST. Callers scope the list to one design group — that is the
 * fix for a bug that shipped: settling globally (across all groups) let a
 * majority section poison a minority group's state. Verified live: 9 blog
 * articles legitimately mark "Blog" active (a section marker, active on
 * every child page — the "active on exactly one page" assumption does not
 * hold), and their global majority overwrote the legal pages' correctly
 * INACTIVE "Blog" link. Per-group, each group settles to its own truth:
 * the articles' group keeps Blog active (right for every page that renders
 * that part), the legal group keeps it inactive, and a single-page group
 * keeps its own state verbatim — which is exactly 1:1 for the one page
 * that part renders on.
 *
 * The vote buckets by DEPTH-FOLDED href, not literal text — "blog.html"
 * and "../blog.html" are the same link, and rewriteLinks resolves both to
 * the same permalink downstream.
 *
 * Input: [{ pages, markup }]. Output: same list, markup settled.
 */
export function settleActiveStates(list) {
  const tally = new Map();
  const globalFreq = new Map();
  const LINK = new RegExp(`<a\\b${TAG_ATTRS}?href=(["'])([^"']*)\\1${TAG_ATTRS}>`, 'gi');
  const fold = (href, pageFile) => rootHref(href, pageFile);

  // Buckets are keyed by the link's POSITION in the region, not by its href.
  //
  // Href is the wrong key the moment a region holds two links to the same
  // target — which is ordinary, not exotic: a header carrying a desktop nav
  // AND a mobile drawer has every target twice, a footer has the wordmark and
  // a big decorative logo both linking home, a nav item and a CTA button both
  // point at /pricing/. Keyed by href, those become "competing values" for one
  // bucket and the loser is REWRITTEN WITH THE WINNER'S CLASS. Verified live on
  // a converted site: every drawer link lost `py-3 text-center` to the desktop
  // nav's class, the drawer's "Buy now" button lost its entire button styling,
  // and the footer's full-width watermark logo was rewritten with the small
  // wordmark's `w-25 h-6` and vanished from the design. All three shipped into
  // parts/header.html and parts/footer.html at build time, so no render-side
  // check could see them — they simply were the theme.
  //
  // Position is exact here because settling only ever runs over markup that is
  // already ONE STRUCTURAL GROUP (grouped by groupKey, which strips volatile
  // classes): link N is the same link on every page of the group. Guarded
  // anyway — if the variants disagree on link count, or on the href at a given
  // index, that index is left entirely alone rather than settled on a
  // misalignment.
  const linksOf = (markup, pageFile) => [...markup.matchAll(LINK)].map((m) => ({
    cls: classOf(m[0]), cur: currentMarkerOf(m[0]), href: fold(m[2], pageFile),
  }));
  const perVariant = list.map((v) => ({ v, links: linksOf(v.markup, v.pages[0]) }));
  const counts = new Set(perVariant.map((x) => x.links.length));
  const aligned = counts.size === 1;
  // The current-page MARKER runs as its own channel, on the same buckets and
  // the same estimator. It is the attribute form of the active class — a
  // standards-written site marks its current nav item `aria-current="page"`
  // and changes no class at all — and absence is what the other links wear,
  // so the global-frequency vote settles the group to "no marker", which is
  // exactly the resting state a shared template part must ship. A group whose
  // pages ALL mark the same link (a section marker) is unanimous and keeps it,
  // the same rule the class channel already follows.
  const curTally = new Map();
  const curFreq = new Map();
  if (aligned) {
    for (const { v, links } of perVariant) {
      links.forEach((l, i) => {
        const cur = l.cur ? l.cur.value : '';
        const ct = curTally.get(i) || new Map();
        ct.set(cur, (ct.get(cur) || 0) + v.pages.length);
        curTally.set(i, ct);
        curFreq.set(cur, (curFreq.get(cur) || 0) + v.pages.length);
        if (!l.cls) return;
        const t = tally.get(i) || new Map();
        t.set(l.cls.value, (t.get(l.cls.value) || 0) + v.pages.length);
        tally.set(i, t);
        globalFreq.set(l.cls.value, (globalFreq.get(l.cls.value) || 0) + v.pages.length);
      });
    }
    // An index whose href is not the same across every variant is not the same
    // link, so it is not a state bucket.
    for (const i of [...tally.keys()]) {
      const hrefs = new Set(perVariant.map((x) => x.links[i] && x.links[i].href));
      if (hrefs.size > 1) tally.delete(i);
    }
    for (const i of [...curTally.keys()]) {
      const hrefs = new Set(perVariant.map((x) => x.links[i] && x.links[i].href));
      if (hrefs.size > 1) curTally.delete(i);
    }
  }
  // Per bucket: a unanimous value stands (a true section group keeps its own
  // state). When values COMPETE, the inactive one wins — estimated as the
  // value shared across the nav's OTHER links (page-weighted global
  // frequency), because sibling links share their resting style while the
  // active marker is the outlier. Plain page-majority is NOT a valid
  // estimator: a section marker is active on every page of its section, and
  // when the section is the biggest page group (9 blog articles vs 7 other
  // pages, hit live) majority bakes the ACTIVE state into everyone's nav.
  const winner = new Map();
  for (const [index, t] of tally) {
    winner.set(index, [...t.entries()].sort((a, b) =>
      (globalFreq.get(b[0]) - globalFreq.get(a[0])) || (b[1] - a[1]))[0][0]);
  }
  const curWinner = new Map();
  for (const [index, t] of curTally) {
    curWinner.set(index, [...t.entries()].sort((a, b) =>
      (curFreq.get(b[0]) - curFreq.get(a[0])) || (b[1] - a[1]))[0][0]);
  }
  return list.map((v) => {
    let i = -1;
    return {
      ...v,
      markup: v.markup.replace(LINK, (full) => {
        i += 1;
        let out = full;
        const cls = classOf(out);
        const w = winner.get(i);
        if (cls && w !== undefined && w !== cls.value) {
          out = out.replace(`class=${cls.quote}${cls.value}${cls.quote}`, `class=${cls.quote}${w}${cls.quote}`);
        }
        const cur = currentMarkerOf(out);
        const cw = curWinner.get(i);
        if (cw === undefined) return out;
        const has = cur ? cur.value : '';
        if (has === cw) return out;
        // Drop the marker, or write the group's unanimous one in its place.
        out = out.replace(CURRENT_ATTR_RE, '');
        if (cw !== '') {
          out = out.replace(/^<a\b/i, `<a ${cur ? cur.name : 'aria-current'}="${cw}"`);
        }
        return out;
      }),
    };
  });
}

// The GROUPING KEY neutralizes everything that is state rather than design:
//  - volatile classes (the site's own JS toggles them — scroll/sticky state)
//  - inline style attributes, wholesale. Verified live: an export froze a
//    per-page animation transform (style="transform: translateY(-0.499%)",
//    a different jitter value on every page) onto one real header design and
//    split it into 11 spurious singleton "variants" — 11 separately-editable
//    parts of the same design, an edit to one propagating to none of the
//    others. Inline styles on chrome are how JS animation drives state (the
//    same reason the pixel gates force-neutralize inline opacity/transform);
//    a genuinely authored inline-style difference merged by this is caught
//    by gate B's per-page pixel diff, and the at-rest capture supplies each
//    group's true resting style either way.
//  - each link's OWN class value (active-nav markers — settled per group
//    AFTER partitioning, see settleActiveStates above)
//  - path depth ("../index.html" vs "index.html" — same target)
const LINK_KEY_RE = new RegExp(`<a\\b${TAG_ATTRS}?href=(["'])[^"']*\\1${TAG_ATTRS}>`, 'gi');
function neutralizeLinkClasses(html) {
  return html.replace(LINK_KEY_RE, (full) => {
    const cls = classOf(full);
    return cls ? full.replace(`class=${cls.quote}${cls.value}${cls.quote}`, `class=${cls.quote}${cls.quote}`) : full;
  });
}
//  - each link's CURRENT-PAGE MARKER. `aria-current="page"` is the attribute
//    form of the active class above — the standards-written way to mark the
//    current nav item, and a site can use it while changing no class at all,
//    so neutralizing classes alone does not see it. Left in the key it splits
//    one header design into one group PER NAV ITEM: measured on a 32-page
//    photography site, 7 groups where the design has 2, and worse, the real
//    variance (a transparent header over hero pages, an opaque one elsewhere)
//    ended up MIXED inside those groups — so a genuine design difference
//    would have been flattened while page state was preserved, precisely
//    backwards. Settled per group by settleActiveStates' second channel.
const CURRENT_ATTR_RE = /\s(aria-current|data-current)=("[^"]*"|'[^']*')/gi;
function currentMarkerOf(tagHtml) {
  const m = /\s(aria-current|data-current)=("([^"]*)"|'([^']*)')/i.exec(tagHtml);
  if (!m) return null;
  return { name: m[1], value: m[3] === undefined ? m[4] : m[3], quote: m[2][0] };
}
function neutralizeCurrentMarkers(html) {
  return html.replace(LINK_KEY_RE, (full) => full.replace(CURRENT_ATTR_RE, ''));
}
//  - insignificant WHITESPACE. hotfix (creative-003): a source hand-formatted
//    with different indentation on one page is not design variance, and this
//    is only a comparison KEY — the markup a group ships still comes from
//    groupMarkup, byte for byte. Verified live: index.html's header and footer
//    are whitespace-normalised IDENTICAL to the other five pages' (2658 vs
//    2667 bytes, 6834 vs 6850) and were split into their own groups anyway, so
//    the theme shipped a duplicate header part whose edits never reached the
//    front page and a footer part rendered by no page at all.
// Class whitespace is not design either, and it is asymmetric by construction:
// stripVolatile returns the attribute verbatim when it removed nothing and
// re-joins on single spaces when it removed something, so the SAME class list
// keys two ways depending on whether that element happened to carry a state
// token. Observed on dexler: class="nav-link inline-flex items-center " (the
// source's own trailing space, left by a templating engine that emitted an
// empty state token) against class="nav-link inline-flex items-center" — one
// space, six pages in a spurious second header group.
const collapseClassWhitespace = (html) => html.replace(
  /\bclass=("([^"]*)"|'([^']*)')/gi,
  (m, _q, dbl, sgl) => {
    const q = dbl === undefined ? "'" : '"';
    const v = (dbl === undefined ? sgl : dbl).trim().split(/\s+/).filter(Boolean).join(' ');
    return `class=${q}${v}${q}`;
  },
);
// Both wave-1 fixes are kept: creative's whitespace neutralisation and
// dexler's link-target resolution. They are not the same fix — one says
// indentation is not design, the other says a link's TARGET is what identifies
// it — and dropping either re-opens a real split. rootTargets replaces the
// earlier `../` strip on purpose: stripping the prefix is the basename-instead-
// of-path family this campaign keeps meeting, and resolving is the fix for it.
const groupKey = (markup, volatile, pageFile) => rootTargets(collapseClassWhitespace(neutralizeCurrentMarkers(neutralizeLinkClasses(
  stripVolatile(markup, volatile).replace(/\sstyle=("[^"]*"|'[^']*')/gi, '')
))), pageFile)
  .replace(/\s+/g, ' ')
  .replace(/>\s+</g, '><')
  .trim();

/**
 * The nav-link class states of a region — { active, rest } — read off the
 * settle vote.
 *
 * Settling writes the resting style everywhere, which is the only value one
 * shared template part can carry — but it erases a per-page fact the source
 * had: which nav link is highlighted on which page. That fact is
 * RECOVERABLE at render time, because WordPress knows the current URL and
 * the plugin rewrites zone links from the menu on every request — it just
 * needs to know what "highlighted" looks like in this design. That is
 * exactly what the vote establishes: in a competed bucket, the winner is
 * the shared resting style (`rest`) and the loser is the active marker
 * (`active`). Both travel to the theme contract — the plugin swaps a
 * link's class to `active` only when the link currently wears `rest` AND
 * its target is the page being served, so a brand link or CTA inside the
 * same zone (different classes) is never touched.
 *
 * Both empty when the region's links never compete (a design with no
 * class-marked active state, or one marked via attributes) — then the
 * contract carries nothing and the plugin restores nothing, which is
 * exactly the no-guessing default.
 */
/**
 * The active/resting nav-link class values for a set of chrome markups.
 *
 * SCOPE MATTERS: the caller must pass markup covering exactly ONE navigation
 * zone. The signal is "the same href wears two different class values across
 * pages" — which is the current-page highlight — and a region holding TWO navs
 * produces that signal spuriously, because each href appears once per nav with
 * each nav's own resting class. Verified live on a header carrying a desktop
 * <ul> and a mobile drawer: nav 1's "active" came out as the DRAWER's resting
 * class, so restoring the highlight would have added `py-3 text-center` to the
 * current page's desktop link and dropped the real `text-primary font-medium`
 * — a visible layout change on every page. make-theme therefore scopes this
 * per zone via findNavZone(); the region-wide call below is only for the
 * whole-region default when no zone is declared.
 */
export function regionNavStates(rawVariants) {
  // Per href, per class value, the SET OF PAGES that wear it — not a count.
  // An active/resting pair is one href appearing with two class values on
  // DIFFERENT pages: resting everywhere, active on the one page it points at.
  // Two values on the SAME page are not that. Verified live inside a single
  // mobile drawer: its "Pricing" nav link and its "Buy now" CTA both point at
  // pricing.html, on every page, so a count-based tally read the CTA's button
  // classes as the active state for the whole menu — and restoring the
  // highlight would have turned the current page's nav link into a filled
  // pill. Requiring the page sets to be disjoint is what makes the pairing
  // mean "same link, different page" rather than "two links, same target".
  const tally = new Map();
  const globalFreq = new Map();
  const LINK = new RegExp(`<a\\b${TAG_ATTRS}?href=(["'])([^"']*)\\1${TAG_ATTRS}>`, 'gi');
  const fold = (href, pageFile) => rootHref(href, pageFile);
  for (const v of rawVariants) {
    for (const m of v.markup.matchAll(LINK)) {
      const cls = classOf(m[0]);
      if (!cls) continue;
      const t = tally.get(fold(m[2], v.pages[0])) || new Map();
      const seen = t.get(cls.value) || new Set();
      for (const pg of v.pages) seen.add(pg);
      t.set(cls.value, seen);
      tally.set(fold(m[2], v.pages[0]), t);
      globalFreq.set(cls.value, (globalFreq.get(cls.value) || 0) + v.pages.length);
    }
  }
  const rests = new Map();
  const actives = new Map();
  const disjoint = (a, b) => { for (const x of a) if (b.has(x)) return false; return true; };
  for (const [, t] of tally) {
    if (t.size < 2) continue;
    const ranked = [...t.entries()].sort((a, b) =>
      (globalFreq.get(b[0]) - globalFreq.get(a[0])) || (b[1].size - a[1].size));
    const [restValue, restPages] = ranked[0];
    const candidates = ranked.slice(1).filter(([, pages]) => disjoint(pages, restPages));
    if (!candidates.length) continue; // two links to one target, not a state pair
    rests.set(restValue, (rests.get(restValue) || 0) + restPages.size);
    for (const [value, pages] of candidates) {
      actives.set(value, (actives.get(value) || 0) + pages.size);
    }
  }
  const ranked = (m) => [...m.entries()].sort((a, b) => b[1] - a[1]);
  const top = (m) => (m.size ? ranked(m)[0][0] : '');
  // A menu with two LEVELS legitimately has one active/resting pair PER LEVEL
  // — dexler's four top-level items rest as `nav-link block false` and its six
  // dropdown children as `nav-dropdown-link block false`. The contract carries
  // ONE pair, so the vote used to return whichever level outscored the other
  // and the plugin then found no link wearing the losing level's rest class:
  // the current-page highlight silently vanished for the whole other level
  // (dexler-011, bigspring-015 / mc-016+mc-024). Choosing correctly needs a
  // pair-per-level contract, which is a plugin change — so until then the ONE
  // thing this function must not do is pick silently. Every vocabulary that
  // carries real weight (>= half the winner's pages) is returned, and the
  // consumer reports the loser by name.
  const restRanked = ranked(rests);
  const restVocabularies = restRanked
    .filter(([, n]) => n >= (restRanked[0]?.[1] || 0) / 2)
    .map(([value, pages]) => ({ value, pages }));
  return { active: top(actives), rest: top(rests), restVocabularies };
}

/**
 * The partition itself: per region, the design groups in falling page-count
 * order. Each group: { index, pages, markup, variants } — markup is the
 * group's majority variant settled to the GROUP's own active-nav truth
 * (stage 3 still prefers a per-group at-rest capture over it — a group is
 * a design, not a frozen scroll state); variants is the settled member
 * list, so stage 3 can settle the at-rest capture against this group and
 * only this group. `active` maps each region to its { active, rest } nav
 * class states (see regionNavStates above).
 *
 * @param {object} MF  conversion manifest
 * @param {string} WS  workspace path
 * @returns {{ regions: Record<string, Array>, active: Record<string, {active:string,rest:string}> }}
 */
export function computeChromeGroups(MF, WS) {
  const astroReport = JSON.parse(readFileSync(join(WS, 'astro-report.json'), 'utf8'));
  const fragDir = join(WS, 'astro-project', 'src', 'fragments', 'chrome');
  const VOL = volatileClasses(resolve(MF.input.dir));
  const variants = astroReport.chromeVariants || [];
  const manifestPages = Array.isArray(MF.pages)
    ? new Set(MF.pages.map((page) => page.file))
    : null;
  for (const [index, variant] of variants.entries()) {
    if (!variant || !/^[a-z0-9][a-z0-9_-]{0,96}$/.test(variant.id || '') ||
        !/^[a-z0-9][a-z0-9_-]{0,96}$/.test(variant.region || '') ||
        !Array.isArray(variant.pages) ||
        !variant.pages.every((page) => typeof page === 'string' &&
          (!manifestPages || manifestPages.has(page)))) {
      throw new Error(`unsafe chromeVariants[${index}] in astro-report.json`);
    }
  }

  const regions = {};
  const active = {};
  // Kept so a caller can recompute states over a NARROWER scope than the
  // region — see the regionNavStates docstring.
  const rawByRegion = {};
  const regionNames = [...new Set(variants.map((v) => v.region))];
  for (const region of regionNames) {
    const raw = variants.filter((v) => v.region === region)
      .map((v) => {
        const fragment = safePathUnderRoot(fragDir, `${v.id}.html`);
        if (!fragment) throw new Error(`chrome fragment path leaves its root: ${JSON.stringify(v.id)}`);
        return { pages: v.pages.slice(), markup: readFileSync(fragment, 'utf8') };
      });
    rawByRegion[region] = raw;
    active[region] = regionNavStates(raw);
    // The current-page marker is not always carried by an <a>. This design
    // puts it on the dropdown TOGGLE — <span class="nav-link … active"> on the
    // six pages the dropdown links to — and neutralizeLinkClasses only blanks
    // anchors, so those six headers keyed as their own design group. The vote
    // above has already named the marker: whatever tokens differ between the
    // active and resting class values ARE the state, wherever they are worn.
    // Stripped from the KEY only; the markup itself is untouched and settling
    // still decides what each group renders.
    const stateTokens = new Set();
    {
      const A = new Set((active[region].active || '').split(/\s+/).filter(Boolean));
      const R = new Set((active[region].rest || '').split(/\s+/).filter(Boolean));
      for (const t of A) if (!R.has(t)) stateTokens.add(t);
      for (const t of R) if (!A.has(t)) stateTokens.add(t);
    }
    const KEY_VOL = stateTokens.size ? new Set([...VOL, ...stateTokens]) : VOL;
    const groups = new Map();
    for (const v of raw) {
      const key = groupKey(v.markup, KEY_VOL, v.pages[0]);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(v);
    }
    regions[region] = [...groups.values()]
      .map((members) => {
        const settled = settleActiveStates(members);
        const best = settled.reduce((a, b) => (b.pages.length > a.pages.length ? b : a));
        return {
          pages: settled.flatMap((m) => m.pages),
          markup: best.markup,
          variants: settled,
        };
      })
      .sort((a, b) => b.pages.length - a.pages.length)
      .map((g, i) => ({ index: i, ...g }));
  }
  return { regions, active, raw: rawByRegion };
}

// ---------- CLI: write chrome-groups.json for the python capture step ----------

const invokedDirectly = process.argv[1] && import.meta.url.endsWith(process.argv[1].split('/').pop());
if (invokedDirectly && process.argv.some((a) => a.startsWith('--manifest='))) {
  const manifestPath = process.argv.find((a) => a.startsWith('--manifest=')).slice(11);
  const MF = JSON.parse(readFileSync(manifestPath, 'utf8'));
  const WS = resolve(MF.workspace);
  const { regions } = computeChromeGroups(MF, WS);
  // Markup stays out of the JSON — the capture step reads chrome from the
  // LIVE page, and make-theme imports this module directly. The JSON names
  // the partition: which pages form each group.
  const out = {
    regions: Object.fromEntries(Object.entries(regions).map(([r, gs]) => [
      r, gs.map((g) => ({ index: g.index, pages: g.pages })),
    ])),
  };
  writeFileSync(join(WS, 'chrome-groups.json'), JSON.stringify(out, null, 2));
  const summary = Object.entries(regions).map(([r, gs]) => `${r}: ${gs.length}`).join(', ');
  console.log(`OK — chrome design groups (${summary}) → ${join(WS, 'chrome-groups.json')}`);
}
