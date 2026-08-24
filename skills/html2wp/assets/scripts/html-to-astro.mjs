#!/usr/bin/env node
/**
 * Stage 1 — turn a flat HTML directory into an Astro 5 project, per the
 * conversion manifest.
 *
 * THE ONE ARCHITECTURAL RULE: the site's own HTML is never written into
 * `.astro` template syntax. An `.astro` file is a JSX-like language, not
 * HTML — `{` opens an expression, `<Capitalized>` is a component, and the
 * compiler re-parses whatever it sees. Pasting arbitrary third-party markup
 * into that is a category error, and it corrupts real content: a JSON code
 * sample in a blog post (`{ "colors": { … } }`) either fails the build or
 * survives only by being entity-escaped into `&lbrace;`, which is then what
 * WordPress stores and what a reader copies out of the code block.
 *
 * Instead every fragment is written to a real `.html` file under
 * `src/fragments/` and pulled in with Vite's `?raw` import, then emitted
 * through `<Fragment set:html={…} />`. Astro treats that string as opaque:
 *
 *   - literal { } survive (JSON/JS/CSS samples, `{variable}` in prose)
 *   - entities survive exactly (&amp; stays &amp;, no decode round-trip)
 *   - <style> is NOT scoped, <script> is NOT bundled — so `is:inline`,
 *     the old #1 failure mode, stops being a concern at all
 *   - whitespace is byte-preserved
 *
 * Verified empirically against Astro 5 before this was written.
 *
 * What each page owns vs. what is shared:
 *   - <head>: per page, VERBATIM. Sharing one page's head across all pages
 *     bakes its OG tags into every page and needs title/description props,
 *     whose escaping is its own bug farm. A nested page's own head also
 *     already carries link/script paths correct for ITS depth — verbatim
 *     means zero path rewriting.
 *   - chrome (header + trailing nodes): taken from EACH PAGE ITSELF and
 *     deduped, so byte-identical chrome collapses to one shared fragment
 *     and a page whose chrome differs keeps its own. Nothing is rewritten
 *     or neutralised here — that would be a design change, and it belongs
 *     to stage 3, where WordPress's single template part forces it.
 *   - body: per page, verbatim minus the chrome that was cut out.
 *
 * A page marked chrome:"self-contained" is copied straight into `public/`,
 * which Astro passes through untouched — the strongest fidelity guarantee
 * available, and it needs no layout by definition.
 *
 *   node html-to-astro.mjs --manifest=conversion-manifest.json
 *
 * Reads manifest.input.dir, writes manifest.workspace/astro-project/.
 * Exit 0 = project written; 1 = error.
 */

import { readFileSync, writeFileSync, mkdirSync, readdirSync, statSync, cpSync, rmSync, existsSync, openSync, readSync, closeSync } from 'node:fs';
import { join, dirname, resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { entryKind, safePathUnderRoot } from './lib/safe-path.mjs';
import { assetVerdict, secretContentReason, strictAssetMode, SCAN_LIMIT_BYTES } from './lib/secret-filter.mjs';

// A tag's attribute run may legitimately contain '>' inside a quoted
// value (Tailwind: class="[&>svg]:size-3"). Matching attributes with
// [^>]* truncates such a tag and every downstream read of it fails.
const TAG_ATTRS = `(?:[^>"']|"[^"]*"|'[^']*')*`;

const args = process.argv.slice(2);
const manifestPath = (args.find((a) => a.startsWith('--manifest=')) || '').slice(11);
if (!manifestPath) die('usage: html-to-astro.mjs --manifest=conversion-manifest.json');
const MF = JSON.parse(readFileSync(manifestPath, 'utf8'));
const INPUT = resolve(MF.input.dir);
const WS = resolve(MF.workspace);
const PROJ = join(WS, 'astro-project');

const FRAG = join(PROJ, 'src', 'fragments');

// Clean slate for everything this script generates — but NOT node_modules.
// Re-running stage 1 is the normal loop while a conversion is being tuned,
// and wiping the install turned every iteration into a full npm install.
for (const e of existsSync(PROJ) ? readdirSync(PROJ) : []) {
  if (e === 'node_modules') continue;
  rmSync(join(PROJ, e), { recursive: true, force: true });
}
mkdirSync(PROJ, { recursive: true });
for (const d of [
  ['src', 'pages'], ['src', 'lib'],
  ['src', 'fragments', 'chrome'], ['src', 'fragments', 'heads'], ['src', 'fragments', 'bodies'],
  ['public'],
]) mkdirSync(join(PROJ, ...d), { recursive: true });

const report = { pages: [], warnings: [], componentFiles: [] };

/** Entries walk() refused to follow: symlinks, device nodes, sockets. */
const skippedLinks = [];

// `pages[].file` is written into the manifest by an assistant reading someone
// else's project, and this script both READS it (join(INPUT, file)) and WRITES
// three paths derived from it — public/, src/pages/, src/fragments/. `join`
// collapses `..` silently, so an entry like `../../../.ssh/config` read from
// outside the input and wrote outside the workspace, on the user's own machine.
//
// Vetted once, here, so the three write helpers below stay simple: a path that
// survives this is relative, has no `..` left in it, and resolves inside the
// input directory. An unsafe entry is dropped with a warning rather than
// stopping the conversion — same rule as the payload filter.
if (Array.isArray(MF.pages)) {
  const before = MF.pages.length;
  MF.pages = MF.pages.filter((p) => {
    if (safePathUnderRoot(INPUT, p?.file)) return true;
    report.warnings.push(`page path refused as unsafe and skipped: ${JSON.stringify(p?.file)}`);
    return false;
  });
  if (MF.pages.length !== before) {
    console.log(`  ${before - MF.pages.length} page(s) dropped — see astro-report.json`);
  }
  if (MF.pages.length === 0) die('no usable pages left in the manifest after path checks');
}

// ---------- public/ — every non-HTML web file, original paths preserved ----------
//
// "Every non-HTML file" is what this used to be, filtered by four directory
// names. Everything else in the folder the user pointed at came too — .env,
// id_rsa, credentials.json, .npmrc — into public/, then into dist/, then into
// the tarball uploaded to the service. See lib/secret-filter.mjs.

// Read at most `n` bytes from the head of a file. A secret pasted into a large
// log or dump must still be scanned, but reading the whole file to find a
// marker in its first kilobyte would pull a multi-megabyte dump into memory for
// nothing.
function readHead(abs, n) {
  const fd = openSync(abs, 'r');
  try {
    const buf = Buffer.allocUnsafe(n);
    const read = readSync(fd, buf, 0, n, 0);
    return buf.subarray(0, read);
  } finally {
    closeSync(fd);
  }
}

const STRICT_ASSETS = strictAssetMode();
const skipped = [];

walk(INPUT, (abs) => {
  const rel = abs.slice(INPUT.length + 1);
  if (/\.html?$/i.test(rel)) return;

  const verdict = assetVerdict(rel, { strict: STRICT_ASSETS });
  if (!verdict.copy) {
    // Silent for the noise everyone expects to be dropped; reported for
    // anything a person might go looking for later.
    if (!/(^|\/)(node_modules|\.git|\.astro|_original)\//.test(rel) && !rel.endsWith('.DS_Store')) {
      skipped.push(`${rel} — ${verdict.reason}`);
    }
    return;
  }

  let size = 0;
  try { size = statSync(abs).size; } catch { /* unreadable: cpSync will say so */ }
  const contentReason = secretContentReason(rel, () => readHead(abs, SCAN_LIMIT_BYTES), size);
  if (contentReason) {
    skipped.push(`${rel} — ${contentReason}`);
    return;
  }

  cpSync(abs, join(PROJ, 'public', rel));
});

for (const note of skipped) report.warnings.push(`not uploaded: ${note}`);
for (const link of skippedLinks) report.warnings.push(`not uploaded: ${link} — it is a symlink or a special file`);
if (skipped.length || skippedLinks.length) {
  console.log(`  ${skipped.length + skippedLinks.length} file(s) left out of the conversion payload (see astro-report.json)`);
}

// A site's top chrome is not always a <header>. Kinto's is a <nav
// class="fixed inset-x-0 top-0">; only its two legal pages carry a
// <header> at all, and that one is a page title inside the content. So the
// element is named by the manifest, defaulting to the common case.
const HEADER_SEL = MF.chrome?.header?.selector || 'header';

const consensusPages = MF.pages.filter((p) => p.chrome === 'consensus');
const selfContained = MF.pages.filter((p) => p.chrome === 'self-contained');

// ---------- chrome variants: one fragment per DISTINCT chrome, deduped ----------
//
// The Astro build's job is to BE the site, so chrome is taken from each page
// ITSELF and shared only where pages are byte-identical. Forcing one
// canonical header onto every page — what this stage used to do — is a
// design change, and on a real site it was a big one: 14 of 18 pages ship a
// floating `fixed top-0 …` header over their hero while the rest use an
// in-flow `relative` one, an 11–17% pixel difference. That is parametric
// variance the source deliberately authored, not drift to be flattened.
//
// Canonicalisation still has to happen — WordPress renders ONE header
// template part — but it belongs to stage 3, against the target that
// actually forces it, where it can be reported as a deliberate decision.
// Doing it here would only hide the variance behind an earlier gate.
//
// Byte-identical chrome still collapses to a single shared fragment, so the
// dedup benefit is kept wherever it is real rather than asserted.
const variants = new Map(); // region -> Map(content -> {id, pages[]})
const chromeOf = (region, content, file) => {
  if (!variants.has(region)) variants.set(region, new Map());
  const forRegion = variants.get(region);
  if (!forRegion.has(content)) {
    forRegion.set(content, { id: `${region}--${sha8(content)}`, pages: [] });
  }
  const v = forRegion.get(content);
  v.pages.push(file);
  return v.id;
};

// Some exports put the shared footer inside their content wrapper (often
// `<main>`), while the manifest still records it as the canonical footer
// chrome. Treat that explicit footer selector as the default trailing
// component when no multi-node trailing component was declared, so stage 1
// and make-theme agree about where the footer lives.
const trailingSpecs = MF.chrome?.trailing?.length
  ? MF.chrome.trailing
  : (MF.chrome?.footer?.selector
    ? [{ component: 'SiteFooter', selectors: [MF.chrome.footer.selector] }]
    : []);

// ---------- consensus pages ----------

for (const page of consensusPages) {
  const html = readFileSync(join(INPUT, page.file), 'utf8');
  // Split at <body>, keeping BOTH halves whole: the head half is the entire
  // document prefix (doctype, <html> with its own attributes, the full
  // <head>) and the body half runs to end of file. Nothing is normalised or
  // rebuilt, so each page keeps its own <html>/<body> attributes — which are
  // load-bearing often enough (a font-scoping class generated onto <body>,
  // a color-scheme on <html>) that sharing one page's version across all
  // pages is a real regression, not a tidy-up.
  const { head: pagePrefix, body } = split(html);
  let pageHead = pagePrefix;

  // body: swap this page's OWN chrome nodes for markers IN PLACE. The
  // fragment they point at is this page's own markup, so the rebuilt page
  // is byte-identical to its source; pages whose chrome matches simply
  // resolve to the same shared fragment.
  let rest = body;
  const hdr = findBySelector(rest, HEADER_SEL);
  if (!hdr) report.warnings.push(`${page.file}: no ${HEADER_SEL} found — emitted whole body`);
  else {
    const id = chromeOf('header', hdr.outer, page.file);
    rest = rest.slice(0, hdr.start) + `<!--CHROME:${id}-->` + rest.slice(hdr.start + hdr.outer.length);
  }

  trailingSpecs.forEach((spec, i) => {
    // hotfix (simple-002): trailing chrome is what follows the content
    // region, so look for it AFTER </main> FIRST. findBySelector returns the
    // FIRST match, and a `<footer>` selector otherwise claims the first
    // CARD footer inside <main> — this site's blog cards and testimonial
    // cards use <footer> for their "Read more" row.
    //
    // The rule that hotfix meant is capture-chrome.py's: a match INSIDE
    // <main> is not chrome. Implemented as "after </main>" it also excluded
    // the region BEFORE it, which is where a menu overlay lives — the
    // ordinary shape of a header toggle plus its full-screen panel, emitted
    // as a sibling between </header> and <main>. Stage 2.5 captured that
    // overlay (it is outside main) and stage 4 cut it from every page source
    // (it cuts by selector anywhere), while this stage left it in the body:
    // three stages, two answers. Measured on a 32-page photography site
    // whose `div.menu` carries three of its seven navigation groups. So the
    // leading region is searched before falling back inside <main>.
    // One REGION at a time, in that order — never a selector here and a
    // selector there. The span below runs from the first match to the last,
    // so a component matched on both sides of <main> would cut the entire
    // page content into the chrome fragment.
    const mainNode = firstTag(rest, 'main');
    const mainEnd = mainNode ? mainNode.start + mainNode.outer.length : 0;
    const regions = mainNode
      ? [{ at: mainEnd, html: rest.slice(mainEnd) }, { at: 0, html: rest.slice(0, mainNode.start) }]
      : [{ at: 0, html: rest }];
    let found = [];
    for (const region of regions) {
      const hits = [];
      for (const sel of spec.selectors) {
        const node = findBySelector(region.html, sel);
        if (node) hits.push({ ...node, start: node.start + region.at });
      }
      if (hits.length) { found = hits; break; }
    }
    // A footer nested in <main> is still shared chrome in the source. Only
    // use this fallback after both outside-main searches above, and choose
    // the last matching node so a card's earlier <footer> byline is not cut
    // out in place of the site's actual footer.
    if (!found.length && mainNode) {
      for (const sel of spec.selectors) {
        const inside = allBySelector(mainNode.outer, sel);
        const last = inside[ inside.length - 1 ];
        if (last) found.push({ ...last, start: mainNode.start + last.start });
      }
    }
    if (!found.length) { report.warnings.push(`${page.file}: no node matched ${spec.selectors.join(', ')}`); return; }
    // A component can own several sibling nodes (footer + veil + drawer).
    // The fragment is the exact SPAN of the document from the first to the
    // last of them, in document order — not the matches concatenated in
    // selector order with an invented '\n' between them. Joining that way
    // both reordered the nodes when the manifest listed selectors out of
    // document order AND left the real inter-node whitespace behind in the
    // body, so the rebuilt page was no longer byte-identical.
    found.sort((a, b) => a.start - b.start);
    const spanStart = found[0].start;
    const last = found[found.length - 1];
    const spanEnd = last.start + last.outer.length;
    const span = rest.slice(spanStart, spanEnd);
    // Last line of defence for the region rule above: a span that contains
    // the content region would publish the whole page as chrome on every
    // page. Refuse and say so — a missing chrome fragment is a warning the
    // next stage reports, a swallowed <main> is a destroyed conversion.
    if (mainNode && spanStart <= mainNode.start && spanEnd >= mainEnd) {
      report.warnings.push(
        `${page.file}: ${spec.selectors.join(' + ')} span the content region — refused, chrome not cut`);
      return;
    }
    // Anything between the matched nodes travels with them. If that is more
    // than whitespace, the selectors are not contiguous and real page
    // content would be pulled into the chrome — say so rather than do it
    // quietly.
    let covered = 0;
    for (const n of found) covered += n.outer.length;
    const between = span.length - covered;
    if (between > 0) {
      const gaps = span.slice(0, spanEnd - spanStart);
      const nonWs = found.slice(0, -1).some((n, k) =>
        rest.slice(n.start + n.outer.length, found[k + 1].start).trim() !== '');
      if (nonWs) {
        report.warnings.push(
          `${page.file}: ${spec.selectors.join(' + ')} are not adjacent — ${between} bytes between them ` +
          `travel into the ${spec.component} fragment`);
      }
      void gaps;
    }
    const id = chromeOf(`trailing-${i}`, span, page.file);
    rest = rest.slice(0, spanStart) + `<!--CHROME:${id}-->` + rest.slice(spanEnd);
  });

  // Some Next.js static exports leak page metadata into the BODY near
  // </html> instead of <head> — a real streaming-metadata artifact, seen on
  // a live shadcn/ui site where 9 of 10 pages had NO <title> in <head> at
  // all and the only copy sat after the real </footer>. That metadata IS
  // the page's authored metadata, so it is RELOCATED into the head rather
  // than deleted: deleting it would leave those pages with no title, and
  // leaving it in place gives the document two.
  const moved = relocateStrayMetadata(rest, pageHead);
  rest = moved.body;
  pageHead = moved.head;

  const key = page.file.replace(/\.html?$/i, '');
  writeFrag(`heads/${key}.html`, pageHead);
  writeFrag(`bodies/${key}.html`, rest);

  const up = '../'.repeat(page.file.split('/').length);
  writePage(page.file, `---
import { shell } from '${up}lib/shell.mjs';
import head from '${up}fragments/heads/${key}.html?raw';
import body from '${up}fragments/bodies/${key}.html?raw';
---
<Fragment set:html={shell(head, body)} />
`);
  report.pages.push({ file: page.file, mode: 'consensus', bytes: rest.length });
}

// ---------- chrome fragments + the shell, now that every variant is known ----------

const allVariants = [];
for (const [region, forRegion] of variants) {
  for (const [content, v] of forRegion) {
    writeFrag(`chrome/${v.id}.html`, content);
    allVariants.push({ ...v, region, pages: v.pages.slice() });
    report.componentFiles.push(`${v.id}.html`);
  }
}
report.chromeVariants = allVariants.map(({ id, region, pages }) => ({ id, region, pages }));
for (const [region, forRegion] of variants) {
  if (forRegion.size > 1) {
    report.warnings.push(
      `${region}: ${forRegion.size} distinct variants across pages — each page keeps its own here, ` +
      `but WordPress renders ONE ${region} template part, so stage 3 must choose and report it`
    );
  }
}

writeFileSync(join(PROJ, 'src', 'lib', 'shell.mjs'), `// Generated by html-to-astro.mjs.
//
// Every fragment arrives as an opaque string via Vite's ?raw import, so the
// site's markup is never parsed as .astro template syntax.
//
// A page fragment is the ORIGINAL document, split in two, with each chrome
// node swapped for a marker comment IN PLACE. Rebuilding is concatenation
// plus marker substitution — this file invents no bytes of its own (no
// added newlines, no re-serialised tags), so every page comes out
// byte-identical to its source. Cutting chrome out and re-appending it in a
// fixed order instead would silently REORDER whatever sat beside it:
// verified live, a leading <div hidden=""></div> ahead of the header moved
// to after it on all 18 pages of a real site.
${allVariants.map((v, i) => `import v${i} from '../fragments/chrome/${v.id}.html?raw';`).join('\n')}

const CHROME = {
${allVariants.map((v, i) => `  '${v.id}': v${i},`).join('\n')}
};

export function shell(head, body) {
  return head + body.replace(/<!--CHROME:([a-z0-9-]+)-->/g, (m, k) => CHROME[k] ?? '');
}
`);

// ---------- self-contained pages: straight into public/, untouched ----------

for (const page of selfContained) {
  const dest = join(PROJ, 'public', page.file);
  mkdirSync(dirname(dest), { recursive: true });
  cpSync(join(INPUT, page.file), dest);
  report.pages.push({ file: page.file, mode: 'self-contained (public/, verbatim)', bytes: statSync(dest).size });
}

// ---------- config + package ----------

writeFileSync(join(PROJ, 'astro.config.mjs'), `// @ts-check
import { defineConfig } from 'astro/config';

// format:'preserve' — the build must MIRROR the input's own file layout,
// because every page's head, links and asset refs are carried verbatim and
// are therefore correct only at the depth the source wrote them. 'directory'
// emits about/index.html and breaks every "x.html" link; 'file' is right for
// a flat site but COLLAPSES a directory index — src/pages/blog/index.astro
// lands at dist/blog.html, one level up from where blog/index.html's own
// "../styles.css" resolves, so a nested site builds every directory index
// unstyled. 'preserve' is byte-for-byte identical to 'file' on a flat site
// (about.astro → about.html) and correct on a nested one.
// compressHTML:false — whitespace-sensitive inline rows, and the output must
// stay diffable against the original.
export default defineConfig({
  build: { format: 'preserve' },
  compressHTML: false,
});
`);

writeFileSync(join(PROJ, 'package.json'), JSON.stringify({
  name: `${MF.site.slug}-astro`,
  private: true,
  type: 'module',
  scripts: { dev: 'astro dev', build: 'astro build', preview: 'astro preview' },
  // Pinned exactly, not a caret range.
  //
  // Astro IS the build for every conversion this pipeline produces, so
  // `^5.0.0` meant the thing under the gates changed whenever upstream
  // published — and a fidelity regression traced to a minor release nobody
  // chose is indistinguishable, from inside a gate report, from one caused by
  // the site. The same argument the test compose file makes at length for its
  // Docker digests applies here with more force: that WordPress is a fixture,
  // this is the compiler.
  //
  // Overridable for the case where a site genuinely needs a newer Astro, and
  // raised deliberately (with a run of the fixtures) rather than by drift.
  // 5.18.2 is what `^5.0.0` resolved to on the day this was pinned, so the
  // pin changes nothing today and stops it changing tomorrow.
  dependencies: { astro: process.env.H2WP_ASTRO_VERSION || '5.18.2' },
}, null, 2));

writeFileSync(join(PROJ, '.gitignore'), 'node_modules/\ndist/\n.astro/\n.DS_Store\n');

writeFileSync(join(WS, 'astro-report.json'), JSON.stringify(report, null, 2));
console.log(`OK — ${report.pages.length} pages (${selfContained.length} self-contained), ` +
  `${allVariants.length} chrome fragment(s) across ${variants.size} region(s), ` +
  `${report.warnings.length} warning(s) → ${PROJ}`);
for (const w of report.warnings) console.warn('  warn: ' + w);

// ---------- helpers ----------

function split(html) {
  const at = html.search(/<body[\s>]/i);
  return { head: at > -1 ? html.slice(0, at) : '', body: at > -1 ? html.slice(at) : html };
}

function writeFrag(rel, contents) {
  const dest = join(FRAG, rel);
  mkdirSync(dirname(dest), { recursive: true });
  writeFileSync(dest, contents);
}


// A page file can carry a subdirectory (blog/some-article.html). The single
// src/pages mkdir above does not create it.
function writePage(sourceFile, contents) {
  const dest = join(PROJ, 'src', 'pages', sourceFile.replace(/\.html?$/i, '.astro'));
  mkdirSync(dirname(dest), { recursive: true });
  writeFileSync(dest, contents);
}


// See the call site: <title>/<meta> found in a page BODY is a build-tool
// leak, never authored body content. Moved into the head, skipping anything
// the head already declares, so the page ends up with its metadata exactly
// once. <meta itemprop> is real body content (microdata) and stays put.
function relocateStrayMetadata(body, head) {
  const moved = [];
  // An SVG carries its own <title> as its accessible name, and <noscript>
  // legitimately holds <meta>. Both are page CONTENT — only tags sitting at
  // the top level of the body are the export leak this relocates. Masking
  // those containers first is what keeps a "Open menu" icon label from
  // being deleted outright (it would then be skipped on re-insert too,
  // because the head already has a <title>).
  const guarded = [];
  let out = body.replace(/<(svg|noscript)\b[\s\S]*?<\/\1>/gi, (b) => {
    guarded.push(b);
    return `\x00G${guarded.length - 1}\x00`;
  });
  out = out.replace(/<title\b[^>]*>[\s\S]*?<\/title>/gi, (m) => { moved.push(m); return ''; });
  out = out.replace(/<meta\b(?![^>]*\bitemprop=)[^>]*>/gi, (m) => { moved.push(m); return ''; });
  out = out.replace(/\x00G(\d+)\x00/g, (_, i) => guarded[+i]);
  if (!moved.length) return { body, head };

  let newHead = head;
  for (const tag of moved) {
    if (/^<title/i.test(tag)) {
      if (/<title[\s>]/i.test(newHead)) continue;
    } else {
      const id = (tag.match(/\b(?:name|property|charset|http-equiv)=["']?([^"'\s>]+)/i) || [])[1];
      if (id) {
        const dup = new RegExp(`<meta\\b[^>]*\\b(?:name|property|charset|http-equiv)=["']?${id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}["'\\s>]`, 'i');
        if (dup.test(newHead)) continue;
      }
    }
    // The head fragment is the whole document prefix, so it ENDS with
    // </head> — appending would land the tag outside the head entirely.
    const close = newHead.toLowerCase().lastIndexOf('</head>');
    newHead = close === -1 ? newHead + tag : newHead.slice(0, close) + tag + newHead.slice(close);
  }
  return { body: out, head: newHead };
}

// A class="..." value routinely contains an embedded OPPOSITE quote —
// Tailwind's arbitrary-value syntax nests one freely, e.g.
// [&_svg:not([class*='text-'])] inside a double-quoted attribute. A
// same-quote backreference (["'])…\1 stops at that inner character and
// silently fails; two disjoint alternatives never fall into that trap.
function classOf(tagHtml) {
  const d = tagHtml.match(/\bclass="([^"]*)"/i);
  if (d) return { quote: '"', value: d[1] };
  const s = tagHtml.match(/\bclass='([^']*)'/i);
  if (s) return { quote: "'", value: s[1] };
  return null;
}

function idOfTag(tagHtml) {
  const m = tagHtml.match(/\bid="([^"]*)"/i) || tagHtml.match(/\bid='([^']*)'/i);
  return m ? m[1] : '';
}

// The manifest names chrome with a selector, and until now that could only be
// "tag" or "tag.class". A real site's top chrome need not carry a class at
// all: aubergine's floating pill nav is `<div id="nav-wrapper">` on 42 pages
// and `<div id="nav-wrapper" class="fixed inset-x-0 z-50 top-2 lg:top-8">` on
// the other 6, so no single tag.class addresses it — `div.fixed` matches the
// search modal first, and bare `div` matches the grid background. Without an
// id form there is NO selector for that site's header, which is not a
// judgment call the operator can make differently; it is a hole in the
// vocabulary. Tag is required before the '#': firstTag() builds the closing
// tag regex from it.
// hotfix (creative-002): `:not(.class)` joins the vocabulary. A site whose
// site footer is a bare <footer> and whose blog CARDS carry
// <footer class="…"> bylines has no positive selector for the site footer at
// all — first-match picks a card byline, and every stage then treats it as
// chrome. Valid CSS, so capture-chrome.py's querySelector reads it too.
function parseSelector(sel) {
  const m = /^([A-Za-z][\w-]*)(?:#([\w-]+))?(?:\.([\w-]+))?(?::not\(\.([\w-]+)\))?$/.exec(String(sel || '').trim());
  if (!m) return { tag: String(sel || '').split('.')[0], cls: String(sel || '').split('.')[1], id: '', notCls: '' };
  return { tag: m[1], id: m[2] || '', cls: m[3] || '', notCls: m[4] || '' };
}


// First <tag …>…</tag> with depth counting.
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

// "tag", "tag.class" or "tag#id" — first matching element.
function findBySelector(html, sel) {
  const { tag, cls, id, notCls } = parseSelector(sel);
  let from = 0;
  for (;;) {
    const node = firstTag(html, tag, from);
    if (!node) return null;
    if (!cls && !id && !notCls) return node;
    const openTag = node.outer.match(new RegExp(`^<${TAG_ATTRS}>`))[0];
    const classAttr = (classOf(openTag) || { value: '' }).value;
    const okCls = !cls || classAttr.split(/\s+/).includes(cls);
    const okId = !id || idOfTag(openTag) === id;
    const okNot = !notCls || !classAttr.split(/\s+/).includes(notCls);
    if (okCls && okId && okNot) return node;
    from = node.start + 1;
  }
}

function allBySelector(html, sel) {
  const matches = [];
  let from = 0;
  for (;;) {
    const node = findBySelector(html.slice(from), sel);
    if (!node) return matches;
    const absolute = { ...node, start: from + node.start };
    matches.push(absolute);
    from = absolute.start + Math.max(1, absolute.outer.length);
  }
}

function walk(dir, fn) {
  for (const e of readdirSync(dir)) {
    const p = join(dir, e);
    // entryKind uses lstat and returns null for links and special files — see
    // lib/safe-path.mjs. statSync here meant `assets -> ~/.ssh` read as a
    // directory, and everything on the other side was copied into public/.
    const kind = entryKind(p);
    if (kind === 'dir') walk(p, fn);
    else if (kind === 'file') fn(p);
    else if (kind === null) skippedLinks.push(p.slice(INPUT.length + 1));
  }
}

function sha8(s) {
  return createHash('sha1').update(s).digest('hex').slice(0, 8);
}

function die(msg) { console.error('ERROR: ' + msg); process.exit(1); }
