#!/usr/bin/env node
/**
 * Gate A2 — STRUCTURAL 1:1 parity, per page and per region.
 *
 * Gate A proves the build LOOKS like the source (pixels). This proves it IS
 * the source (markup), region by region: <header>, its <nav>, <footer>, and
 * <main>. The two catch different things — a pixel diff cannot see a lost
 * aria-label, a dropped data-attribute, a link that quietly changed target,
 * or a nav item that vanished below the fold. Both gates run; neither
 * replaces the other.
 *
 *   node verify-parity.mjs --manifest=conversion-manifest.json \
 *     [--original=<dir>] [--dist=<dir>] [--out=parity-report.json]
 *
 * Two transformations the pipeline makes on purpose are normalised away on
 * BOTH sides before comparing, because they are corrections, not drift:
 *
 *   1. Path depth. The same target is written differently depending on the
 *      depth of the page writing it ("about.html" at the root,
 *      "../about.html" one directory down). Both sides are resolved to a
 *      root-absolute path first, so those compare equal when they denote
 *      the same file.
 *   2. Metadata position. A <title>/<meta> that a static export leaked into
 *      the BODY is relocated into <head>. Metadata is therefore compared as
 *      a SET over the whole document, not by position.
 *
 * Everything else is a real difference and is reported. Chrome that the
 * manifest deliberately canonicalised is listed under `canonicalized` —
 * expected, but never silently: it is the one thing a WordPress theme
 * cannot reproduce per-page, since header/footer become ONE shared
 * template part, and the owner has to be told which pages it changes.
 *
 * Exit 0 = every page 1:1 outside declared canonicalisation. 1 = not.
 */

import { readFileSync, existsSync, writeFileSync } from 'node:fs';
import { join, resolve, dirname, posix } from 'node:path';

const args = process.argv.slice(2);
const one = (n, d = '') => (args.find((a) => a.startsWith(`--${n}=`)) || `--${n}=${d}`).slice(n.length + 3);
const manifestPath = one('manifest');
if (!manifestPath) die('usage: verify-parity.mjs --manifest=conversion-manifest.json');
const MF = JSON.parse(readFileSync(manifestPath, 'utf8'));
const ORIG = resolve(one('original', MF.input.dir));
const DIST = resolve(one('dist', join(MF.workspace, 'astro-project', 'dist')));
const OUT = one('out', join(MF.workspace, 'parity-report.json'));

// 'header' here means "whatever element the manifest calls the top chrome";
// a site whose chrome is a <nav> would otherwise have it compared as the
// generic nav region and its real chrome never checked at all.
const HEADER_SEL = MF.chrome?.header?.selector || 'header';
// The footer selector comes from the manifest for the same reason the header
// does. It used to be the bare tag, so on a site whose bottom chrome is a
// <div> — every div-chrome site in the queue, 45 of 176 — findBySelector
// matched nothing on either side, the region was skipped as "absent on both",
// and the gate still reported it as one of the regions it checked. No
// div-chrome site has ever had its footer structurally compared.
const FOOTER_SEL = MF.chrome?.footer?.selector || 'footer';
// Same family as the footer selector above, one step further in: a site with
// no <main> at all has never had its CONTENT structurally compared — only its
// chrome. The content region is therefore manifest-nameable too, and the
// special value "between-chrome" means "everything from the end of the header
// region to the start of the footer region", which is the only answer on a
// site whose pages wrap their content in a different tag each
// (<section class="w-full"> on some, <div> on others). Default unchanged.
const CONTENT_SEL = MF.chrome?.content?.selector || 'main';
// The nav region was the last one still hardcoded to a bare tag, and it is the
// tag a pre-HTML5 design is LEAST likely to use: a Bootstrap 3 site writes its
// menu as <div class="navbar"><ul class="nav">, so `nav` matched nothing on
// either side of all six pages. Because the coverage rule below (correctly)
// fails a region that was never compared, such a site could not pass gate A2 at
// all — not for any drift, but because the gate addressed an element the design
// does not contain. Same remedy as header/footer/content: name the real
// element in the manifest. Default unchanged.
const NAV_SEL = MF.chrome?.nav?.selector || 'nav';
const REGIONS = [HEADER_SEL, NAV_SEL, FOOTER_SEL, CONTENT_SEL].filter((r, i, a) => a.indexOf(r) === i);
// Coverage is counted, not assumed. "50 pages x 4 regions" is a claim about
// work performed, and it was false whenever a selector matched nothing.
const coverage = Object.fromEntries(REGIONS.map((r) => [r, { compared: 0, absentBoth: 0 }]));
const report = { pages: {}, canonicalized: [], unmaterialized: [], passed: true };

// Stage 2.6 --apply writes copy the site kept in JavaScript into the markup, so
// the built page legitimately carries text the source file does not. That is a
// DELIBERATE transformation, the third one (after path rooting and metadata
// position), and this gate knew only the first two — so every conversion that
// materialized anything failed a gate the skill requires to pass with no
// exemptions, for the one reason that is not drift.
//
// The exemption is DERIVED, never declared: stage 2.6 records the exact
// opening tag of every element it filled, and each of those is emptied again
// on the dist side before comparing. The element was empty in the source, so
// after reversing it the two sides are compared byte for byte exactly as
// before. Anything stage 2.6 did not record still fails — this cannot become a
// place to hide real drift.
const MAT = (() => {
  const p = one('materialize', join(MF.workspace, 'materialize-report.json'));
  if (!existsSync(p)) return {};
  try {
    const r = JSON.parse(readFileSync(p, 'utf8'));
    if (!r.applied) return {};
    const byPage = {};
    for (const [file, note] of Object.entries(r.pages || {})) {
      const written = (note.filled || []).filter((d) => d.status === 'written' && d.openFull && d.tag);
      if (written.length) byPage[file] = written;
    }
    return byPage;
  } catch { return {}; }
})();

// Stage 2.65 is the same situation: it gives a submittable `name=` to a field
// that shipped with only an `id`, which is a markup change the source does not
// have. Reversed here from its own record for the same reason.
const FIELDS = (() => {
  const p = one('formfields', join(MF.workspace, 'normalize-form-fields-report.json'));
  if (!existsSync(p)) return {};
  try {
    const r = JSON.parse(readFileSync(p, 'utf8'));
    if (!r.applied) return {};
    const byPage = {};
    for (const [file, note] of Object.entries(r.pages || {})) {
      // Keyed on the RECORD, not on the status word: a field patched inside a
      // SHARED chrome fragment is reported against the page that consumed the
      // fragment's one occurrence ("already handled (shared fragment)"), while
      // the name is in every sharing page's build just the same. The
      // before/after pair is what makes the exemption derived and exactly
      // reversible; the status is only a label on it.
      const named = (note.forms || []).flatMap((f) => f.fields || [])
        .filter((d) => d.openBefore && d.openAfter);
      if (named.length) byPage[file] = named;
    }
    return byPage;
  } catch { return {}; }
})();

/** Restore every element stage 2.6 filled on this page to the empty element it
 *  was in the source, and every field stage 2.65 named to its unnamed form.
 *  Depth-aware for 2.6, because the written content contains same-tag
 *  children. */
function unmaterialize(html, file) {
  for (const d of MAT[file] || []) {
    const at = html.indexOf(d.openFull);
    if (at === -1) continue;
    const el = firstTag(html, d.tag, at);
    if (!el || el.start !== at) continue;
    html = html.slice(0, at) + d.openFull + `</${d.tag}>` + html.slice(el.start + el.outer.length);
    report.unmaterialized.push(`${file}:${d.text.slice(0, 40)}`);
  }
  for (const d of FIELDS[file] || []) {
    if (!html.includes(d.openAfter)) continue;
    html = html.replace(d.openAfter, d.openBefore);
    report.unmaterialized.push(`${file}:name=${d.name}`);
  }
  return html;
}
// Declared here, not beside its function: the main loop below calls
// isCanonicalChrome(), and a `let` in the helpers section would still be in
// its temporal dead zone by then.
let canonCache = null;

for (const page of MF.pages) {
  const o = join(ORIG, page.file);
  const d = join(DIST, page.file);
  if (!existsSync(o) || !existsSync(d)) {
    report.pages[page.file] = { error: !existsSync(o) ? 'missing in original' : 'missing in dist' };
    report.passed = false;
    continue;
  }
  const dir = dirname(page.file) === '.' ? '' : dirname(page.file);
  const oh = readFileSync(o, 'utf8');
  const dh = unmaterialize(readFileSync(d, 'utf8'), page.file);
  const entry = {};

  // metadata as a set — position-independent by design (see header comment)
  const oMeta = metaSet(oh), dMeta = metaSet(dh);
  // A tag counts as SUPERSEDED, not lost, when the built page still
  // declares the same name/property with a different value. That happens
  // legitimately: a document can carry two contradictory directives when a
  // build tool leaked a metadata block into the body — e.g. a 404-design
  // page whose real head says robots=noindex while the leaked block says
  // robots="index, follow". Relocation keeps the head's value, which is the
  // authoritative one; emitting both would be objectively worse than
  // dropping one, so this must not fail the gate. A name that vanishes
  // ENTIRELY still does.
  const nameOf = (t) => (t.match(/\b(?:name|property|charset|http-equiv)=["']?([^"'\s>]+)/i) || [, /^<title/i.test(t) ? 'title' : ''])[1];
  const distNames = new Set([...dMeta].map(nameOf).filter(Boolean));
  const missing = [...oMeta].filter((t) => !dMeta.has(t));
  const lost = missing.filter((t) => { const n = nameOf(t); return !n || !distNames.has(n); });
  const superseded = missing.filter((t) => !lost.includes(t));
  const gained = [...dMeta].filter((t) => !oMeta.has(t));
  if (lost.length || superseded.length || gained.length) {
    entry.metadata = {};
    if (lost.length) { entry.metadata.lost = lost.slice(0, 5); report.passed = false; }
    if (superseded.length) entry.metadata.superseded = superseded.slice(0, 5);
    if (gained.length) entry.metadata.gained = gained.slice(0, 5);
  }

  for (const region of REGIONS) {
    const pick = (html) => (region === 'between-chrome'
      ? betweenChrome(html)
      : findBySelector(html, region)?.outer || '');
    const a = normalize(pick(oh), dir);
    const b = normalize(pick(dh), dir);
    if (!a && !b) { coverage[region].absentBoth++; continue; }
    coverage[region].compared++;
    if (a === b) continue;
    const at = firstDiff(a, b);
    const info = {
      origBytes: a.length, distBytes: b.length,
      at, orig: a.slice(at, at + 100), dist: b.slice(at, at + 100),
    };
    // Is this the manifest's own canonicalisation showing up? The canonical
    // page's chrome is what every page gets, so compare against THAT.
    if ((region === HEADER_SEL || region === 'footer' || region === NAV_SEL) && isCanonicalChrome(region, b, dir)) {
      entry[region] = { ...info, status: 'canonicalized-from-' + (MF.chrome?.header?.canonicalFrom || MF.pages[0].file) };
      report.canonicalized.push(`${page.file}:${region}`);
    } else {
      entry[region] = info;
      report.passed = false;
    }
  }
  if (Object.keys(entry).length) report.pages[page.file] = entry;
}

// A region the gate never once compared is not a clean region — it is a
// selector that addresses nothing, and reporting it as checked is the gate
// lying about its own coverage. This is the third instance of that family
// found in one campaign (gate A took its page list from the build; stage 0
// counted generator sources as pages), so it fails here rather than warning.
report.coverage = coverage;
const uncovered = REGIONS.filter((r) => coverage[r].compared === 0);
if (uncovered.length && MF.pages.length) {
  report.passed = false;
  report.uncoveredRegions = uncovered.map((r) =>
    `${r}: matched nothing on either side across all ${MF.pages.length} pages — the selector addresses no element, so this region was never compared. Name it in manifest.chrome (header.selector / footer.selector) the way b66a31d does for an id.`);
}

writeFileSync(OUT, JSON.stringify(report, null, 2));
const broken = Object.entries(report.pages).filter(([, e]) =>
  e.error || Object.entries(e).some(([k, v]) => k !== 'metadata' && v && !v.status) || (e.metadata?.lost || []).length);
console.log(`${report.passed ? 'GATE A2 PASSED' : 'GATE A2 FAILED'} — ${MF.pages.length} pages, ` +
  `${Object.values(coverage).reduce((n, c) => n + c.compared, 0)} region comparisons ` +
  `(${REGIONS.map((r) => `${r} ${coverage[r].compared}`).join(', ')})` +
  (uncovered.length ? `; NEVER COMPARED: ${uncovered.join(', ')}` : '') +
  (broken.length ? `; failing ${broken.length}: ${broken.slice(0, 6).map(([f]) => f).join(', ')}` : '') +
  (report.canonicalized.length ? `; canonicalized (expected): ${report.canonicalized.length}` : '') +
  (report.unmaterialized.length ? `; stage-2.6 fills reversed before comparing: ${report.unmaterialized.length}` : ''));
for (const [f, e] of broken.slice(0, 8)) {
  if (e.error) { console.log(`  ${f}: ${e.error}`); continue; }
  for (const [region, v] of Object.entries(e)) {
    if (region === 'metadata') { if (v.lost?.length) console.log(`  ${f} lost metadata: ${v.lost[0]}`); continue; }
    if (!v || v.status) continue;
    console.log(`  ${f} <${region}> differs at ${v.at}\n     orig: ${JSON.stringify(v.orig)}\n     dist: ${JSON.stringify(v.dist)}`);
  }
}
process.exit(report.passed ? 0 : 1);

// ---------- helpers ----------

function isCanonicalChrome(region, distRegionHtml, dir) {
  if (!canonCache) {
    const c = readFileSync(join(ORIG, MF.chrome?.header?.canonicalFrom || MF.pages[0].file), 'utf8');
    canonCache = {};
    for (const r of REGIONS) canonCache[r] = findBySelector(c, r)?.outer || '';
  }
  // The canonical page sits at its own depth; compare depth-normalised.
  const canonRef = MF.chrome?.header?.canonicalFrom || MF.pages[0].file;
  const canonDir = dirname(canonRef) === '.' ? '' : dirname(canonRef);
  return normalize(canonCache[region], canonDir) === normalize(distRegionHtml, dir);
}

// Resolve every local href/src to a root-absolute path so that the same
// target written at different depths compares equal.
function normalize(html, pageDir) {
  return html.replace(/\b(href|src)=(["'])([^"']+)\2/gi, (m, attr, q, val) => {
    if (/^(https?:|\/\/|data:|mailto:|tel:|#)/i.test(val)) return m;
    const [path, tail = ''] = val.split(/(?=[#?])/);
    const abs = path.startsWith('/') ? path.slice(1) : posix.normalize(posix.join(pageDir, path));
    return `${attr}=${q}/${abs}${tail}${q}`;
  });
}

function metaSet(html) {
  const all = new Set();
  for (const m of html.matchAll(/<title\b[^>]*>[\s\S]*?<\/title>/gi)) all.add(m[0].trim());
  for (const m of html.matchAll(/<meta\b[^>]*>/gi)) all.add(m[0].trim());
  return all;
}

function firstDiff(a, b) {
  const n = Math.min(a.length, b.length);
  let i = 0;
  while (i < n && a[i] === b[i]) i++;
  return i;
}

function classOf(tagHtml) {
  const d = tagHtml.match(/\bclass="([^"]*)"/i);
  if (d) return { quote: '"', value: d[1] };
  const q = tagHtml.match(/\bclass='([^']*)'/i);
  if (q) return { quote: "'", value: q[1] };
  return null;
}

function idOfTag(tagHtml) {
  const d = tagHtml.match(/\bid="([^"]*)"/i) || tagHtml.match(/\bid='([^']*)'/i);
  return d ? d[1] : '';
}

// The manifest's chrome vocabulary — see html-to-astro.mjs's parseSelector.
// This gate compares the region the MANIFEST calls the header, so it has to
// resolve the same selector form stage 1 cut with, or a site whose chrome is
// id-only is compared as an empty region on both sides and passes vacuously.
// hotfix (creative-002): `:not(.class)` — see html-to-astro.mjs.
function parseSelector(sel) {
  const m = /^([A-Za-z][\w-]*)(?:#([\w-]+))?(?:\.([\w-]+))?(?::not\(\.([\w-]+)\))?$/.exec(String(sel || '').trim());
  if (!m) return { tag: String(sel || '').split('.')[0], cls: String(sel || '').split('.')[1], id: '', notCls: '' };
  return { tag: m[1], id: m[2] || '', cls: m[3] || '', notCls: m[4] || '' };
}

// The content region of a site that has no single content element: the slice
// from the end of the header chrome to the start of the footer chrome. Either
// end falls back to the body boundary when that chrome is absent.
function betweenChrome(html) {
  const bodyOpen = html.match(/<body\b[^>]*>/i);
  let start = bodyOpen ? bodyOpen.index + bodyOpen[0].length : 0;
  let end = (() => { const i = html.search(/<\/body\s*>/i); return i === -1 ? html.length : i; })();
  const hdr = findBySelector(html, HEADER_SEL);
  if (hdr && hdr.start >= start) start = hdr.start + hdr.outer.length;
  const ftr = findBySelector(html, FOOTER_SEL);
  if (ftr && ftr.start > start && ftr.start < end) end = ftr.start;
  return html.slice(start, end);
}

// "tag", "tag.class" or "tag#id" — the manifest names chrome this way.
function findBySelector(html, sel) {
  const { tag, cls, id, notCls } = parseSelector(sel);
  let from = 0;
  for (;;) {
    const node = firstTag(html, tag, from);
    if (!node) return null;
    if (!cls && !id && !notCls) return node;
    const open = node.outer.match(/^<[^>]*>/)[0];
    const classAttr = (classOf(open) || { value: '' }).value;
    const okCls = !cls || classAttr.split(/\s+/).includes(cls);
    const okId = !id || idOfTag(open) === id;
    const okNot = !notCls || !classAttr.split(/\s+/).includes(notCls);
    if (okCls && okId && okNot) return node;
    from = node.start + 1;
  }
}

function firstTag(html, tag, from = 0) {
  const openRe = new RegExp(`<${tag}\\b[^>]*>`, 'ig');
  openRe.lastIndex = from;
  const open = openRe.exec(html);
  if (!open) return null;
  const start = open.index;
  const re = new RegExp(`<${tag}\\b[^>]*>|</${tag}>`, 'gi');
  re.lastIndex = start;
  let depth = 0, m;
  while ((m = re.exec(html))) {
    depth += m[0][1] === '/' ? -1 : 1;
    if (depth === 0) return { outer: html.slice(start, m.index + m[0].length), start };
  }
  return { outer: html.slice(start), start };
}

function die(msg) { console.error('ERROR: ' + msg); process.exit(1); }
