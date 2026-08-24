#!/usr/bin/env node
/**
 * Stage 0 — inventory an input HTML directory and compute everything a
 * machine CAN know, so the AI's judgment pass starts from facts instead of
 * re-reading every file.
 *
 *   node analyze-input.mjs <input-dir> [--out analysis.json]
 *
 * Emits analysis.json: page inventory, chrome consensus + variance,
 * head-invariance split, asset map, form/blog/collection candidates, design
 * tokens, and refusal signals (SPA shell, zero pages). The AI turns this
 * into conversion-manifest.json (see ../MANIFEST.md); this script never
 * decides anything that needs judgment — it measures and reports.
 *
 * Exit codes: 0 = analysis written; 2 = input unusable (the refusal case —
 * an SPA shell or no HTML at all), with the reason inside analysis.json so
 * the orchestrator can explain rather than guess.
 */

import { pageKey, pageKeyOfLink } from './lib/page-key.mjs';
import { proveEquivalent, chooseSurvivor, addressOf } from './lib/page-equivalence.mjs';
import { readFileSync, writeFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { join, basename, extname, resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { entryKind } from './lib/safe-path.mjs';

const args = process.argv.slice(2);
const INPUT = resolve(args.find((a) => !a.startsWith('--')) || '.');
const OUT = (args.find((a) => a.startsWith('--out=')) || '--out=analysis.json').slice(6);

const sha = (s) => createHash('sha256').update(s).digest('hex').slice(0, 12);

// State-describing attributes excluded from structural hashing — see
// structureOf() below. Declared here because the main flow calls
// structureOf() before the helpers section's declarations execute.
const STATE_ATTRS = new Set(['aria-current', 'aria-expanded', 'aria-hidden', 'aria-pressed', 'open', 'hidden']);

// ---------- gather pages ----------

if (!existsSync(INPUT) || !statSync(INPUT).isDirectory()) {
  fail(`input is not a directory: ${INPUT}`);
}

// Recursive: a real site routinely nests pages (blog/some-article.html —
// hit live on a real conversion, ten real articles invisible to a flat
// scan). Every later stage (html-to-astro.mjs's writePage, verify-static's
// rglob, the bundler's own extraction) already handles a path with a
// directory in it; stage 0 seeing fewer pages than actually exist would
// just mean each one skips manifest classification and every gate silently.
const entries = readdirSync(INPUT);
const htmlFiles = [];
walk(INPUT, (f) => {
  if (!/\.html?$/i.test(f)) return;
  const rel = f.slice(INPUT.length + 1);
  // Any HIDDEN directory is tooling, not the site: a real input carries
  // capture references (.reference/), audit output (.audit/), caches. Their
  // pages are named by convention — a dozen `page.html` files — so every one
  // collides on the same key and stage 0 refuses an input that is perfectly
  // fine. Verified on a real template folder: 15 such files, 12 collisions,
  // conversion blocked before it began.
  //
  // A LEADING UNDERSCORE is the same statement in a different dialect — the
  // near-universal "source, not output" marker: Jekyll's _includes/_layouts,
  // Sass's _partial, and Astro's own src/pages rule, which declines to route
  // any path segment starting with one. Two queued sites ship such a
  // directory: gostartup's _content/ (22 pages) and mobit's (28), each a
  // generator's input that the site's own _build.mjs consumes. Nothing links
  // to them — every page and every .js on both sites checked, zero references
  // — and the root already carries the built equivalents.
  //
  // Counting them as pages was silently expensive: they entered the manifest,
  // stage 1 wrote them to src/pages/_content/…, and Astro then declined the
  // whole directory. The build exited 0 and the pages were simply absent.
  // Excluding them HERE is the honest place — they were never pages — rather
  // than teaching a later step to explain their absence.
  if (rel.split('/').some((seg) =>
        seg.startsWith('.') || seg.startsWith('_') || ['node_modules', 'assets'].includes(seg))) return;
  htmlFiles.push(rel);
});
htmlFiles.sort();
const nestedHtml = htmlFiles.filter((f) => f.includes('/'));

// Buildable-project detection: if this looks like a source project rather
// than built output, say so — the orchestrator builds/prerenders first.
const projectSignals = [];
if (entries.includes('package.json')) {
  try {
    const pkg = JSON.parse(readFileSync(join(INPUT, 'package.json'), 'utf8'));
    const deps = { ...pkg.dependencies, ...pkg.devDependencies };
    for (const fw of ['astro', 'next', 'vite', 'react-scripts', 'nuxt', 'svelte']) {
      if (deps && deps[fw]) projectSignals.push(fw);
    }
  } catch { /* unreadable package.json is just absence of a signal */ }
}

if (htmlFiles.length === 0) {
  fail(projectSignals.length
    ? `no top-level HTML; looks like an unbuilt ${projectSignals.join('/')} project — build/prerender it first, then analyze the output directory`
    : 'no top-level HTML files found');
}

// ---------- per-page parse (regex-level, matching the pipeline's own style) ----------

const pages = [];
for (const file of htmlFiles) {
  const html = readFileSync(join(INPUT, file), 'utf8');
  const headEnd = html.search(/<body[\s>]/i);
  const head = headEnd > -1 ? html.slice(0, headEnd) : '';
  const body = headEnd > -1 ? html.slice(headEnd) : html;

  const title = (head.match(/<title[^>]*>([\s\S]*?)<\/title>/i) || [, ''])[1].trim();
  const description = attrOfMeta(head, 'description');
  const og = collectMeta(head, /^og:/);
  const twitter = collectMeta(head, /^twitter:/);
  const jsonldCount = (head.match(/application\/ld\+json/gi) || []).length;

  // Head externals — the exact class of thing the old bundler dropped.
  const headLinks = [...head.matchAll(/<link\b[^>]*>/gi)].map((m) => m[0]);
  const stylesheets = headLinks.filter((l) => /rel=["']?stylesheet/i.test(l)).map(hrefOf);
  const icons = headLinks.filter((l) => /rel=["']?(icon|apple-touch-icon)/i.test(l)).map(hrefOf);
  const preconnects = headLinks.filter((l) => /rel=["']?preconnect/i.test(l)).map(hrefOf);
  const headScripts = [...head.matchAll(/<script\b[^>]*(?:\/>|>[\s\S]*?<\/script>)/gi)].map((m) => m[0]);
  const headScriptSrcs = headScripts.map((s) => (s.match(/src=["']([^"']+)["']/) || [])[1]).filter(Boolean);
  const inlineHeadStyles = (head.match(/<style[\s>]/gi) || []).length;

  // Body shape. Two hashes per chrome element: `hash` is content-exact
  // (normalised whitespace), `struct` strips attribute values and text —
  // so two copies of the same header that differ only in labels, an
  // active-nav marker or an is-sticky class share a struct group and are
  // reported as VARIANTS of one chrome, while a genuinely different design
  // (the self-contained homepage's own nav) lands in its own group.
  const mains = [...body.matchAll(/<main\b[^>]*>/gi)].map((m) => m[0]);
  const headerTag = firstTag(body, 'header');
  const footerTag = firstTag(body, 'footer');
  const leading = leadingBodyNodes(body);
  const trailing = trailingBodyNodes(body);
  const inlineStyles = (body.match(/<style[\s>]/gi) || []).length;
  const inlineScripts = (body.match(/<script\b(?![^>]*\bsrc=)/gi) || []).length;
  const scriptSrcs = [...body.matchAll(/<script\b[^>]*\bsrc=["']([^"']+)["']/gi)].map((m) => m[1]);
  const forms = [...body.matchAll(/<form\b[^>]*>/gi)].map((m) => m[0]);
  const links = [...body.matchAll(/<a\b[^>]*href=["']([^"']+)["']/gi)].map((m) => m[1]);
  const images = [...body.matchAll(/<img\b[^>]*>/gi)];
  const imagesNoAlt = images.filter((m) => !/\balt=/.test(m[0])).length;
  const absoluteUrls = [...new Set(
    [...html.matchAll(/https?:\/\/([a-z0-9.-]+(?::\d+)?)/gi)].map((m) => m[1])
  )];

  pages.push({
    file,
    key: keyFor(file),
    bytes: html.length,
    title,
    description,
    hasOg: Object.keys(og).length > 0,
    hasTwitter: Object.keys(twitter).length > 0,
    jsonldCount,
    head: {
      hash: sha(normalizeWs(head)),
      stylesheets, icons, preconnects,
      scriptSrcs: headScriptSrcs,
      inlineStyles: inlineHeadStyles,
      inlineScriptCount: headScripts.length - headScriptSrcs.length,
    },
    body: {
      mainCount: mains.length,
      mainTag: mains[0] || null,
      headerHash: headerTag ? sha(normalizeWs(headerTag.outer)) : null,
      footerHash: footerTag ? sha(normalizeWs(footerTag.outer)) : null,
      headerStruct: headerTag ? sha(structureOf(headerTag.outer)) : null,
      footerStruct: footerTag ? sha(structureOf(footerTag.outer)) : null,
      leading, trailing,
      inlineStyles, inlineScripts, scriptSrcs,
      formCount: forms.length,
      forms: forms.slice(0, 5),
      internalLinks: links.filter((h) => /^[A-Za-z0-9._-]+\.html?([#?].*)?$/.test(h)),
      externalHosts: absoluteUrls,
      imageCount: images.length,
      imagesNoAlt,
    },
  });
}

// ---------- key collisions (stage-0 error, the bundler is silent about them) ----------

const keyMap = new Map();
const rawCollisions = [];
for (const p of pages) {
  if (keyMap.has(p.key)) rawCollisions.push({ key: p.key, files: [keyMap.get(p.key), p.file] });
  else keyMap.set(p.key, p.file);
}

// A DEPTH DUPLICATE is not a collision — it is one page a static exporter
// wrote twice, once per URL spelling: about.html for the flat address,
// about/index.html for the directory one. Both spell the same key, which is
// correct, and refusing the input for it left 8 of 176 queued sites (4.5%)
// with no way forward at all.
//
// Merging is three acts, not one. Keep the spelling the site's own links use
// (a tie, including nobody linking it, goes to the directory form — that is
// the shape the address takes once the site is WordPress). Every link to the
// dropped spelling resolves to the kept page, which it already does, because
// both spellings derive the same key. And the dropped ADDRESS is recorded for
// the redirect map: an address the site once served must not vanish without a
// trace. WordPress serves /about/ regardless, so the .html spelling needs a
// redirect either way.
//
// The merge runs ONLY on proven duplicates. Equality is a hash over a form
// normalised for the two differences a depth duplicate is allowed to have —
// relative-path depth and <base href> — computed by a module with its own
// canary, because the first measurement of this family failed toward the
// cleaner answer (recon-004). Pairs that fail the proof stay refusals, and
// their message now names both files with evidence AND the manifest override
// that resolves them, because a refusal with no way forward is what kept this
// whole class blocked.
const allHtmlText = new Map(htmlFiles.map((f) => [f, readFileSync(join(INPUT, f), 'utf8')]));
const duplicatePages = [];
const keyCollisions = [];
for (const c of rawCollisions) {
  const [fileA, fileB] = c.files;
  const { equal, flatHash, dirHash } = proveEquivalent(allHtmlText.get(fileA), allHtmlText.get(fileB), fileA, fileB);
  if (!equal) {
    // Per-FILE properties prove the pages DIFFER. They do not decide which one
    // is the site, and on a real input they point the wrong way: dexler-nextjs
    // ships a hand-written site beside a minified export, so the export is 6
    // lines to the hand-written 169, "About Us | Dexler" reads better than
    // "About", and the hand-written side has more words on 4 of 6 pairs. An
    // agent that trusted this message would have converted the wrong site
    // (dexler-001) — it had to leave the message entirely to decide.
    //
    // What decides is RELATIONAL, and stage 0 has all of it already. Two
    // measures, and the first outranks the second:
    //
    //   FRONT PAGE. Where index.html's own links point. On dexler it names
    //   about/index.html twice and about.html never — decisive, and the
    //   opposite of what every per-file field suggests.
    //
    //   INBOUND FROM OUTSIDE the candidate's own set. Raw inbound counts lie
    //   when a set links only itself: dexler's 11 hand-written pages form a
    //   closed clique with 30 links to about.html, all of them from each
    //   other. Crossing links are the ones that carry information.
    //
    // Both spellings of a directory address are counted — "about/" and
    // "about/index.html" — because exporters use both and counting one is how
    // this measurement was wrong the first time it was written.
    const esc = (v) => v.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const spellings = (f) => (/\/index\.html?$/i.test(f) ? [addressOf(f), f] : [f]);
    const linksFrom = (fromFile, target) => {
      const html = allHtmlText.get(fromFile) || '';
      return spellings(target).reduce((n, a) =>
        n + (html.match(new RegExp(`(?:href|src)\\s*=\\s*"[^"]*${esc(a)}"`, 'gi')) || []).length, 0);
    };
    const isDir = (f) => /\/index\.html?$/i.test(f);
    const FRONT = htmlFiles.find((x) => x === 'index.html' || x === 'index.htm');
    const ev = (f) => {
      const t = allHtmlText.get(f);
      const title = (t.match(/<title[^>]*>([^<]*)/i) || [, ''])[1].trim();
      const set = htmlFiles.filter((x) => isDir(x) === isDir(f));
      let outside = 0, inside = 0;
      for (const x of htmlFiles) {
        if (x === fileA || x === fileB) continue;
        const n = linksFrom(x, f);
        if (isDir(x) === isDir(f)) inside += n; else outside += n;
      }
      const fromFront = FRONT ? linksFrom(FRONT, f) : null;
      return `${f} — front page links it ${fromFront === null ? '(no front page found)' : `${fromFront}x`}; ` +
        `${outside} inbound link(s) from OUTSIDE its own set, ${inside} from within it; ` +
        `set of ${set.length} page(s) addressed the same way ` +
        `(${t.length} B, ${t.split('\n').length} lines, "${title}")`;
    };
    keyCollisions.push({
      ...c,
      proven: 'different',
      hashes: { [fileA]: flatHash, [fileB]: dirHash },
      evidence: `${ev(fileA)}  ||  ${ev(fileB)}`,
      resolve: 'These are two DIFFERENT pages that happen to share a key, not one page written twice. Decide at gate 0 and log it: give one its own key via manifest.pages[].key, or leave it out of manifest.pages[]. The converter must not pick. Weigh the RELATIONAL evidence above over size, line count and title, which are formatting artefacts — a minified export is short and a pretty-printed page is long, and neither says which one is the site. Where the FRONT PAGE points outranks everything; after it, links from OUTSIDE a candidate own set, because a set that only links itself proves nothing.',
    });
    continue;
  }

  const { kept, dropped, links, tie } = chooseSurvivor(fileA, fileB, allHtmlText.values());
  duplicatePages.push({
    key: c.key, kept, dropped, hash: flatHash, links,
    droppedAddress: '/' + addressOf(dropped),
    why: `Same page at two addresses (proven: identical after normalising relative depth and <base>). Kept ${kept}${tie ? ' — neither address is linked more, so the deeper one wins, being the shape the URL takes as WordPress' : ' — the site links it more often'}.`,
  });
}
// The dropped copy stops being a page here, where it stops being one — not
// later, where some step would have to explain its absence.
const droppedFiles = new Set(duplicatePages.map((d) => d.dropped));
if (droppedFiles.size) {
  for (let i = pages.length - 1; i >= 0; i--) if (droppedFiles.has(pages[i].file)) pages.splice(i, 1);
}

// ---------- chrome consensus ----------

// STRUCTURAL grouping decides what is "the same chrome" (variants of one
// header share a struct group even when labels/active-nav/sticky classes
// differ); the content-exact hash then enumerates the variants inside each
// group. Variance is reported, never flattened silently.
const headerGroups = groupBy(pages.filter((p) => p.body.headerStruct), (p) => p.body.headerStruct);
const footerGroups = groupBy(pages.filter((p) => p.body.footerStruct), (p) => p.body.footerStruct);
const headHashGroups = groupBy(pages, (p) => p.head.hash);

const consensus = {
  header: describeGroups(headerGroups, pages.length, (p) => p.body.headerHash),
  footer: describeGroups(footerGroups, pages.length, (p) => p.body.footerHash),
  head: describeGroups(headHashGroups, pages.length),
};

const majorityHeaderStruct = consensus.header.groups[0]?.hash || null;

// Majority stylesheet set — the design system consensus pages share.
const sheetSets = groupBy(pages, (p) => JSON.stringify(p.head.stylesheets));
const majoritySheets = new Set(JSON.parse([...sheetSets.entries()].sort((a, b) => b[1].length - a[1].length)[0]?.[0] || '[]'));

// Self-contained = a different chrome STRUCTURE *and* no stylesheet overlap
// with the consensus design system. Chrome variance alone (a drifted footer,
// a missing is-sticky) is not self-containment — those pages still belong to
// the shared layout and their drift goes into the variance report.
const selfContainedCandidates = pages
  .filter((p) => {
    const differentChrome = !p.body.headerStruct || p.body.headerStruct !== majorityHeaderStruct;
    const sheetOverlap = p.head.stylesheets.some((s) => majoritySheets.has(s));
    return differentChrome && !sheetOverlap;
  })
  .map((p) => p.file);

// ---------- SPA refusal signal ----------

// One HTML file, a root div with no content, and a bundled script = an SPA
// shell. Rendering happens client-side; there is nothing 1:1 to convert.
const spaSignals = [];
if (htmlFiles.length === 1) {
  const only = pages[0];
  const bodyText = readFileSync(join(INPUT, only.file), 'utf8')
    .replace(/<script[\s\S]*?<\/script>/gi, '')
    .replace(/<[^>]+>/g, '').trim();
  const allScripts = [...only.body.scriptSrcs, ...only.head.scriptSrcs]; // Vite/CRA put the module script in <head>
  if (bodyText.length < 200 && allScripts.some((s) => /\/assets\/|bundle|chunk|index-[a-z0-9]+\.js/i.test(s))) {
    spaSignals.push('single HTML file with an empty body and a bundled script — this is an SPA shell, not a static site');
  }
}

// ---------- assets ----------

const assets = { images: [], css: [], js: [], fonts: [], video: [], other: [] };
walk(INPUT, (f) => {
  const rel = f.slice(INPUT.length + 1);
  if (rel.startsWith('node_modules/') || rel.split('/').some((seg) => seg.startsWith('.'))) return;
  const ext = extname(f).toLowerCase();
  const put = (k) => assets[k].push(rel);
  if (['.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.avif'].includes(ext)) put('images');
  else if (ext === '.css') put('css');
  else if (['.js', '.mjs'].includes(ext)) put('js');
  else if (['.woff', '.woff2', '.ttf', '.otf'].includes(ext)) put('fonts');
  else if (['.mp4', '.webm', '.mov'].includes(ext)) put('video');
  else if (!/\.html?$/i.test(rel) && !statSync(f).isDirectory()) put('other');
});

// ---------- design tokens ----------

const tokens = [];
let tokenSource = null;
for (const cssRel of assets.css) {
  const css = readFileSync(join(INPUT, cssRel), 'utf8');
  const root = css.match(/:root\s*{([\s\S]*?)}/);
  if (root) {
    const vars = [...root[1].matchAll(/--([a-zA-Z0-9_-]+)\s*:\s*([^;]+);/g)]
      .map((m) => ({ name: m[1], value: m[2].trim() }));
    if (vars.length > tokens.length) { tokens.length = 0; tokens.push(...vars); tokenSource = cssRel; }
  }
}
// Inline <style> :root blocks too (the self-contained homepage case).
for (const p of pages) {
  const html = readFileSync(join(INPUT, p.file), 'utf8');
  const styleBlocks = [...html.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/gi)];
  for (const b of styleBlocks) {
    const root = b[1].match(/:root\s*{([\s\S]*?)}/);
    if (root) {
      const vars = [...root[1].matchAll(/--([a-zA-Z0-9_-]+)\s*:\s*([^;]+);/g)]
        .map((m) => ({ name: m[1], value: m[2].trim() }));
      if (vars.length > tokens.length) { tokens.length = 0; tokens.push(...vars); tokenSource = `${p.file} (inline)`; }
    }
  }
}

// ---------- blog candidates ----------

// Signals only — the AI decides. A listing candidate is a page ≥2 of whose
// internal link targets share its own filename stem as a prefix
// (journal.html → journal-*.html), which is how static blogs name article
// files. A grep for "blog" in the page text is useless: every page's nav
// mentions the blog.
const blogCandidates = [];
for (const p of pages) {
  const stem = p.file.replace(/\.html?$/i, '');
  const targets = [...new Set(p.body.internalLinks.map((h) => h.replace(/[#?].*$/, '')))]
    .filter((t) => keyMap.has(pageKeyOfLink(t, p.file)) && t !== p.file);
  const stemChildren = targets.filter((t) => t.startsWith(stem + '-'));
  if (stemChildren.length >= 2) {
    blogCandidates.push({ listing: p.file, articles: stemChildren, otherTargets: targets.filter((t) => !stemChildren.includes(t)) });
  }
}

// ---------- navigation groups ----------
//
// Every group of links a visitor would call a menu, found STRUCTURALLY —
// a <nav>, a list of links, or a run of adjacent sibling links — never by
// class name (class names are one site's convention; structure is every
// site's). Nothing about the count or the location is assumed: a site with
// six menus produces six entries — header nav, mobile drawer, three footer
// columns, a sidebar. Each becomes a WordPress menu location downstream
// (make-theme registers it, dist-to-bundle carries its links as a real
// menu), so a group missed here is a menu that silently stays hardcoded —
// which is the exact failure this section exists to end.
const navGroupMap = new Map();
for (const p of pages) {
  const html = readFileSync(join(INPUT, p.file), 'utf8');
  const headEnd = html.search(/<body[\s>]/i);
  const body = headEnd > -1 ? html.slice(headEnd) : html;
  const headerNode = firstTag(body, 'header');
  const footerNode = firstTag(body, 'footer');
  const regionOf = (start) => {
    if (headerNode && start >= headerNode.start && start < headerNode.start + headerNode.outer.length) return 'header';
    if (footerNode && start >= footerNode.start && start < footerNode.start + footerNode.outer.length) return 'footer';
    return 'body';
  };
  const claimed = [];
  const isClaimed = (s, e) => claimed.some(([a, b]) => s >= a && e <= b);

  const record = (node, tag) => {
    const open = node.outer.match(/^<[^>]*>/)[0];
    const links = linksOf(node.outer);
    if (links.length < 2) return;
    claimed.push([node.start, node.start + node.outer.length]);
    // Candidate class tokens must be CSS-safe (a Tailwind token like
    // "group/navigation-menu" or "lg:flex" is not addressable as .class),
    // and the FIRST token is routinely a utility ("flex") shared by half
    // the page — so try every safe token and prefer one that matches this
    // tag exactly once. Best-effort either way: the generated theme stamps
    // each group with data-ve-nav at stage 3, so wirability never depends
    // on the site's class discipline; this selector is for the report and
    // for hand-written themes.
    const cls = (classOf(open) || { value: '' }).value.split(/\s+/)
      .filter((c) => /^[A-Za-z0-9_-]+$/.test(c));
    const countFor = (c) => (c
      ? [...body.matchAll(new RegExp(`<${tag}\\b[^>]*class=["'][^"']*\\b${c.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'gi'))].length
      : [...body.matchAll(new RegExp(`<${tag}\\b`, 'gi'))].length);
    const uniqueCls = cls.find((c) => countFor(c) === 1);
    const chosen = uniqueCls || cls[0] || '';
    const selector = chosen ? `${tag}.${chosen}` : tag;
    const matches = countFor(chosen);
    const key = `${selector}|${regionOf(node.start)}|${links.map((l) => l.href).join(',')}`;
    if (!navGroupMap.has(key)) {
      navGroupMap.set(key, {
        selector,
        selectorUnique: matches === 1,
        region: regionOf(node.start),
        // Every real menu's links carry text; a group of image/card links
        // (an instagram grid, a portfolio) shows up here as textless and is
        // for the judgment pass to reject, not a menu to wire.
        allTextual: links.every((l) => l.text),
        links,
        pages: [],
      });
    }
    navGroupMap.get(key).pages.push(p.file);
  };

  // 1) every <nav> — the explicit case.
  for (let from = 0; ; ) {
    const nav = firstTag(body, 'nav', from);
    if (!nav) break;
    record(nav, 'nav');
    from = nav.start + nav.outer.length;
  }

  // 2) a list most of whose items are links, outside any <nav>.
  for (const tag of ['ul', 'ol']) {
    for (let from = 0; ; ) {
      const list = firstTag(body, tag, from);
      if (!list) break;
      from = list.start + list.outer.length;
      if (isClaimed(list.start, from)) continue;
      const items = [...list.outer.matchAll(/<li\b/gi)].length;
      const linked = [...list.outer.matchAll(/<li\b(?:[^<]|<(?!\/?li\b))*?<a\b/gi)].length;
      if (items >= 2 && linked / items >= 0.6) record(list, tag);
    }
  }

  // 3) a run of ≥3 adjacent sibling links with no other markup between them
  //    — a footer column or an unwrapped header nav. The enclosing element
  //    is found by scanning classed containers, so the selector addresses
  //    the container rather than an unaddressable run.
  for (const m of body.matchAll(/<(div|section|aside|header|footer|span|p)\b((?:[^>"']|"[^"]*"|'[^']*')*)>/gi)) {
    const start = m.index;
    if (isClaimed(start, start + 1)) continue;
    const node = firstTag(body, m[1].toLowerCase(), start);
    if (!node || node.start !== start) continue;
    const inner = node.outer.replace(/^<[^>]*>/, '').replace(/<\/[a-z0-9-]+>$/i, '');
    // Direct children that are links, measured cheaply: the inner must be a
    // whitespace-separated run of <a>…</a> for at least 3 links in a row.
    const run = inner.match(/^\s*(?:<a\b(?:[^>"']|"[^"]*"|'[^']*')*>[\s\S]*?<\/a>\s*){3,}$/i);
    if (run) record(node, m[1].toLowerCase());
  }
}
const navGroups = [...navGroupMap.values()].sort((a, b) =>
  a.region === b.region ? b.pages.length - a.pages.length : a.region.localeCompare(b.region));

// ---------- pseudo-element census (decorative content:"" = invisible to the editor) ----------

let decorativePseudo = 0;
for (const cssRel of assets.css) {
  const css = readFileSync(join(INPUT, cssRel), 'utf8');
  decorativePseudo += (css.match(/::(before|after)\s*{[^}]*content\s*:\s*(""|'')/g) || []).length;
}

// ---------- write ----------

const analysis = {
  schema: 'html2wp-analysis/1',
  input: { dir: INPUT, pages: htmlFiles.length, nestedHtml, projectSignals },
  refusal: spaSignals,
  errors: keyCollisions.map((c) => c.proven === 'different'
    ? `key collision (proven DIFFERENT pages, not a depth duplicate): ${c.files.join(' + ')} → "${c.key}". ${c.evidence}. ${c.resolve}`
    : `key collision: ${c.files.join(' + ')} → "${c.key}"`),
  // Merges are recorded, never assumed: the pair, the hash that proved them
  // equal, the link counts that chose the survivor, and the address that now
  // needs a redirect. An audit trail is the difference between a merge and a
  // silent drop.
  duplicatePages,
  pages,
  consensus,
  selfContainedCandidates,
  assets: Object.fromEntries(Object.entries(assets).map(([k, v]) => [k, { count: v.length, files: v.slice(0, 50) }])),
  design: { tokenSource, tokens, decorativePseudoCount: decorativePseudo },
  navGroups,
  blogCandidates,
  headExternalsUnion: [...new Set(pages.flatMap((p) => [...p.head.stylesheets, ...p.head.scriptSrcs, ...p.head.icons]))],
  externalHostsUnion: [...new Set(pages.flatMap((p) => p.body.externalHosts))],
  proseOnlyInScripts: proseOnlyInScripts(INPUT, htmlFiles),
};

writeFileSync(OUT, JSON.stringify(analysis, null, 2));
const ok = spaSignals.length === 0 && keyCollisions.length === 0;
console.log(`${ok ? 'OK' : 'REFUSED'} — ${htmlFiles.length} pages, consensus header ${consensus.header.groups[0]?.count || 0}/${pages.length}, ` +
  `${selfContainedCandidates.length} self-contained candidate(s), ${tokens.length} design tokens, ` +
  `${navGroups.length} navigation group(s), ${blogCandidates.length} blog candidate(s) → ${OUT}`);
for (const g of navGroups.filter((x) => !x.selectorUnique)) {
  console.warn(`  nav group "${g.selector}" (${g.region}) is not uniquely addressable on its page — ` +
    `the manifest's nav entry needs a selector that matches exactly one element, or the menu cannot be wired`);
}
if (analysis.proseOnlyInScripts.length) {
  console.warn(`NOT EDITABLE — ${analysis.proseOnlyInScripts.length} block(s) of copy live in JavaScript and in no ` +
    `HTML file, so they render on the page but cannot be edited after conversion. Say so at handover:`);
  for (const p of analysis.proseOnlyInScripts.slice(0, 5)) console.warn(`    ${p.file}: "${p.text.slice(0, 72)}…"`);
}
if (!ok) { for (const s of [...spaSignals, ...analysis.errors]) console.error('  ' + s); process.exit(2); }

// ---------- helpers ----------

// Must stay character-for-character identical to dist-to-bundle.mjs's
// keyFor: stage 0 and stage 4 each run their own collision detector, and
// two different key functions mean the two detectors disagree about what
// collides. Case-insensitive 'index', and per-character replacement (not
// collapsing +) on both sides.
// Delegates to the ONE derivation in lib/page-key.mjs. This used to key a
// page by its filename BASENAME, which is fine on a flat site and fatal on a
// nested one: blog/index.html, team/index.html and work/index.html all became
// "front-page". Verified backward-compatible before the change — 95 keys
// across the eight sites converted so far are byte-identical, because all
// eight are flat and a flat file's relative path IS its basename.
function keyFor(relPath) {
  return pageKey(relPath);
}

function walk(dir, fn) {
  for (const e of readdirSync(dir)) {
    const p = join(dir, e);
    // lstat-based, so a symlink is skipped rather than followed out of the
    // input directory — see lib/safe-path.mjs. This walk counts and reads
    // the site's pages, so following one both inflates the count and reads
    // files that are not the site.
    const kind = entryKind(p);
    if (kind === 'dir') { if (!e.startsWith('.') && 'node_modules' !== e) walk(p, fn); }
    else if (kind === 'file') fn(p);
  }
}

function normalizeWs(s) { return s.replace(/\s+/g, ' ').trim(); }

// Structure only: tag names + attribute NAMES, no values, no text. Two
// headers differing in labels, hrefs, an active-nav marker or an is-sticky
// class produce the same structure; a different nav design does not.
// State-describing attributes are excluded outright — aria-current exists
// only on the current page's own nav item, so counting it would split one
// header into "pages that mark an active item" vs "pages that don't".
function structureOf(s) {
  // Attribute NAMES legitimately contain digits, colons and dots — data-v-1,
  // xlink:href, x-on:click. A name class of [a-zA-Z-]+ makes the whole tag
  // fail this match, so it vanishes from the structural signature entirely
  // and two structurally identical chromes stop grouping together.
  const tags = [...s.matchAll(/<\/?([a-zA-Z0-9-]+)((?:\s+[a-zA-Z_:][\w:.-]*(?:=(?:"[^"]*"|'[^']*'|\S+))?)*)\s*\/?>/g)]
    .map((m) => {
      // Must consume each attribute's =VALUE while scanning for the next
      // NAME, or a multi-token class value (Tailwind's own active-nav-link
      // convention: extra utility classes appended to whichever link
      // matches the current page, e.g. class="... bg-accent font-semibold")
      // has each space-separated class token misread as its own attribute
      // name — hashing "today's active page" into the structural signature
      // and fragmenting one real header into as many groups as there are
      // pages. Verified live: this exact pattern split a single consensus
      // header into 6 false singleton groups on a real shadcn/ui site.
      const attrs = [...m[2].matchAll(/\s+([a-zA-Z_:][\w:.-]*)(?:=(?:"[^"]*"|'[^']*'|\S+))?/g)]
        .map((a) => a[1]).filter((a) => !STATE_ATTRS.has(a)).sort().join(',');
      return (m[0][1] === '/' ? '/' : '') + m[1] + (attrs ? '[' + attrs + ']' : '');
    });
  return tags.join('>');
}

function attrOfMeta(head, name) {
  // Attribute order is not fixed — match the tag, then read both attrs.
  for (const m of head.matchAll(/<meta\b[^>]*>/gi)) {
    const tag = m[0];
    const n = (tag.match(/\bname=["']([^"']+)["']/i) || [])[1];
    if (n && n.toLowerCase() === name) return (tag.match(/\bcontent=["']([^"']*)["']/i) || [, ''])[1];
  }
  return '';
}

function collectMeta(head, re) {
  const out = {};
  for (const m of head.matchAll(/<meta\b[^>]*>/gi)) {
    const tag = m[0];
    const key = (tag.match(/\b(?:property|name)=["']([^"']+)["']/i) || [])[1];
    if (key && re.test(key)) out[key] = (tag.match(/\bcontent=["']([^"']*)["']/i) || [, ''])[1];
  }
  return out;
}

function hrefOf(linkTag) { return (linkTag.match(/href=["']([^"']+)["']/i) || [, ''])[1]; }

// First <tag …>…</tag> at or after `from`, with a depth counter (nesting
// honoured, regex-level).
function firstTag(html, tag, from = 0) {
  const openRe = new RegExp(`<${tag}\\b[^>]*>`, 'ig');
  openRe.lastIndex = from;
  const open = openRe.exec(html);
  if (!open) return null;
  const start = open.index;
  const re = new RegExp(`<${tag}\\b[^>]*>|</${tag}>`, 'gi');
  re.lastIndex = start;
  let depth = 0; let m;
  while ((m = re.exec(html))) {
    depth += m[0][1] === '/' ? -1 : 1;
    if (depth === 0) return { outer: html.slice(start, m.index + m[0].length), start };
  }
  return { outer: html.slice(start), start };
}

// class="…" of an opening tag, either quote style. A class value routinely
// embeds the opposite quote (Tailwind arbitrary values), so two disjoint
// alternatives instead of a same-quote backreference.
function classOf(tagHtml) {
  const d = tagHtml.match(/\bclass="([^"]*)"/i);
  if (d) return { quote: '"', value: d[1] };
  const s = tagHtml.match(/\bclass='([^']*)'/i);
  if (s) return { quote: "'", value: s[1] };
  return null;
}

// Every link inside a fragment, document order: { text, href }.
function linksOf(html) {
  return [...html.matchAll(/<a\b((?:[^>"']|"[^"]*"|'[^']*')*)>([\s\S]*?)<\/a>/gi)].map((m) => ({
    href: (m[1].match(/\bhref=["']([^"']*)["']/i) || [, ''])[1],
    text: m[2].replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim(),
  }));
}

// Tag names of the first / last few top-level-ish body elements — enough for
// the AI to see multi-node trailing chrome (footer + veil + drawer).
function leadingBodyNodes(body) {
  const m = [...body.matchAll(/<(header|nav|div|aside|section|main|footer)\b[^>]*>/gi)].slice(0, 4);
  return m.map((x) => x[0].slice(0, 80));
}
function trailingBodyNodes(body) {
  const cut = body.slice(-4000);
  const m = [...cut.matchAll(/<(footer|aside|div|script)\b[^>]*>/gi)].slice(-5);
  return m.map((x) => x[0].slice(0, 80));
}

function groupBy(arr, fn) {
  const map = new Map();
  for (const x of arr) {
    const k = fn(x);
    if (!map.has(k)) map.set(k, []);
    map.get(k).push(x);
  }
  return map;
}

function describeGroups(map, total, variantHashFn) {
  const groups = [...map.entries()]
    .map(([hash, members]) => {
      const g = { hash, count: members.length, files: members.map((p) => p.file) };
      if (variantHashFn) {
        // Content-exact variants inside one structural group — the drift the
        // AI must canonicalize and report.
        const variants = groupBy(members, variantHashFn);
        g.variants = [...variants.entries()]
          .map(([vh, ms]) => ({ hash: vh, count: ms.length, files: ms.map((p) => p.file) }))
          .sort((a, b) => b.count - a.count);
      }
      return g;
    })
    .sort((a, b) => b.count - a.count);
  return { distinct: groups.length, total, groups };
}


// ---------- prose that lives in JavaScript, not in any HTML file ----------
//
// A component-driven template routinely keeps some copy in a JS array and
// renders it at runtime: FAQ answers behind an accordion, testimonial
// quotes in a carousel, tab panel bodies. The conversion carries it
// faithfully — the script ships with the theme and the words still appear
// on the page — but that text is in NO stored source, so the owner cannot
// edit it, and nothing else in this pipeline can tell: pixel gates match
// (both sides run the same script), markup gates match, and the collections
// popup can only offer the fields that exist in the markup.
//
// Verified live: a converted site's five FAQ answers lived in app.js. The
// editor offered the questions as an editable list and had nothing at all
// to show for the answers, and the owner found that out by clicking.
//
// Detected statically — no browser, no interaction, so copy that only
// appears after a click is still found. A quoted string of at least this
// many characters that reads as prose (has spaces and sentence punctuation)
// and appears in none of the HTML files is content the site is hiding in
// its script.
function proseOnlyInScripts(dir, htmlFiles) {
  const MIN = 60;
  const haystack = htmlFiles.map((f) => readFileSync(join(dir, f), 'utf8')).join('\n');
  const jsFiles = [];
  walk(dir, (p) => { if (p.endsWith('.js') && !p.includes('/.audit/')) jsFiles.push(p); });

  const found = [];
  const seen = new Set();
  for (const abs of jsFiles) {
    const js = abs.slice(dir.length + 1);
    let src;
    try { src = readFileSync(abs, 'utf8'); } catch { continue; }
    // A single-line quoted literal. Newlines excluded on purpose: without
    // that, a run starting at one quote swallows hundreds of characters of
    // CODE before finding the next matching quote, and the "prose" this
    // reports is a fragment of the script itself.
    for (const m of src.matchAll(/(["'`])((?:\\.|(?!\1)[^\\\n]){60,800})\1/g)) {
      // The copy is frequently wrapped in markup — the accordion answer that
      // prompted this is `'<div class="...">…</div>'` — so strip tags FIRST
      // and judge what is left. Rejecting anything containing '<' (the
      // obvious first rule) throws away exactly the case worth reporting.
      const text = m[2]
        .replace(/\\n/g, ' ')
        .replace(/<[^>]*>/g, ' ')
        .replace(/\\(['"`])/g, '$1')
        .replace(/\s+/g, ' ')
        .trim();
      if (text.length < MIN || seen.has(text)) continue;
      const words = text.split(' ').filter(Boolean);
      if (words.length < 10 || !/[.!?]/.test(text)) continue;
      // Still code-shaped after stripping tags, or a URL, or a CSS rule.
      if (/[{}=;]|https?:|=>|\bfunction\b|\breturn\b|\bconst\b|\bvar\b/.test(text)) continue;
      // Words, not identifiers: real copy is mostly alphabetic.
      const alpha = words.filter((w) => /^[A-Za-z][A-Za-z'’,.\-]*$/.test(w)).length;
      if (alpha / words.length < 0.8) continue;
      // Already in the markup somewhere? Then it is editable and not a finding.
      if (haystack.includes(text.slice(0, 48))) continue;
      seen.add(text);
      found.push({ file: js, text: text.slice(0, 140) });
    }
  }
  return found;
}

function fail(msg) {
  writeFileSync(OUT, JSON.stringify({ schema: 'html2wp-analysis/1', refusal: [msg] }, null, 2));
  console.error('REFUSED — ' + msg);
  process.exit(2);
}
