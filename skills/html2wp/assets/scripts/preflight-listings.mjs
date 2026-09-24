#!/usr/bin/env node
/**
 * The blog's and the shop's selectors, checked on the built site before the
 * service runs them.
 *
 * Stage 4.5 (build-posts) and stage 4.6 (build-products) turn a listing into
 * live posts or products from `cardContainer`. They take the FIRST element it
 * matches on the listing's stored source (findFirst) and look for the
 * `cardSelector` cards inside it. When that fails, the stage only warns, in a
 * report that never comes back to the workspace. The listing stays static,
 * and make-zip refuses the theme at the very end, with nothing to act on. The
 * other selectors fail the same quiet way: a field that matches nothing is
 * left unplaced, and every post or product prints the specimen's value.
 * Measured causes:
 *   - a selector outside the manifest's grammar, which parses to nothing. The
 *     grammar has tag, .class, #id, :not(.class) and direct `>` paths; it has
 *     no descendant (space) combinator, the form a browser's devtools copies;
 *   - a first match that is another element, such as a mobile menu panel
 *     carrying the same utility classes as the card grid.
 * This checks every blog/shop selector field with the stages' own engine
 * (lib/selector.mjs, the mirror of the service's) on astro-project/dist, and
 * refuses before the upload with the field, the rule, the counts it measured
 * and, when exactly one exists, a `>` path that works.
 *   - Every field is inside the grammar.
 *   - cardContainer: on each listing page its first match holds cards and no
 *     other match does (one grid, the one the stage wires).
 *   - Every other field matches at least one element where it applies: the
 *     card (cardCategory), the article or product pages (articleMain,
 *     articleCategory, articleNav, productMain), inside that region
 *     (articleBody, productBody, productPrice, productGallery, productCategory,
 *     addToCart), or any page (cartLink, cartCount).
 * A page with shared chrome is read as the service stores it: without the
 * first header and trailing chrome outside <main>. A self-contained page is
 * read whole.
 *
 *   node preflight-listings.mjs --manifest=conversion-manifest.json
 *
 * Writes preflight-listings.json beside the manifest (rows in the shape
 * html-finish.py reports). Exit 0: nothing to refuse (notes may be printed).
 * Exit 1: refusals. A manifest that declares no blog and no shop is a no-op.
 */
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { dirname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseSelector, findFirst, findAll } from './lib/selector.mjs';

// Where each field is looked for: `card` inside the wired container's cards,
// `pages` on the article/product pages, `region` inside the article/product
// region, `any` on any page.
const FIELDS = {
  blog: { cardContainer: 'listing', cardSelector: 'listing', cardCategory: 'card', articleMain: 'pages',
    articleBody: 'region', articleCategory: 'pages', articleNav: 'pages' },
  shop: { cardContainer: 'listing', cardSelector: 'listing', productMain: 'pages', productBody: 'region',
    productPrice: 'region', productGallery: 'region', productCategory: 'region', addToCart: 'region',
    cartLink: 'any', cartCount: 'any' },
};
const SECTIONS = {
  blog: { listing: 'listing', card: 'a.post-card', stage: 'stage 4.5 (build-posts)', token: '[wp-posts]',
    pages: (section) => section.articles, main: 'articleMain' },
  shop: { listing: 'shop', card: 'a', stage: 'stage 4.6 (build-products)', token: '[wp-products]',
    pages: (section) => section.products, main: 'productMain' },
};

const tagOf = (open) => (/^<([a-z0-9-]+)/i.exec(open) || [, ''])[1].toLowerCase();

/** Why a selector is outside the grammar. */
function grammarRule(selector) {
  const parts = selector.split(/(?<!\\)>/).map((part) => part.trim());
  if (parts.some((part) => /\s/.test(part.replace(/\\[0-9a-fA-F]{1,6}\s/g, '')))) {
    return 'descendant combinator (a space) not supported; use `>` paths';
  }
  if (/[+~,\[*]/.test(selector.replace(/\[[^\]]*\]/g, (m) => (/^\[[^=\]]*\]$/.test(m) ? '' : m)))) {
    return 'only tag, .class, #id, :not(.class) and `>` are supported (no +, ~, commas, attributes or *)';
  }
  return 'each step must be tag, .class (several allowed), #id or :not(.class)';
}

/** The cards the stage would find in a container: elements the card selector
 * names that are the card's own tag (the stage scans for that tag's opening). */
function cardsIn(outer, card) {
  const tag = ((/^([a-z0-9]+)/i.exec(card) || [, 'a'])[1]).toLowerCase();
  return findAll(outer, card).filter((node) => tagOf(node.open) === tag).length;
}

/** The page as the service stores it: a page with shared chrome loses its
 * first header and trailing chrome found outside <main> (dist-to-bundle). */
function storedView(html, page, manifest) {
  if (!page || page.chrome === 'self-contained') return html;
  const chrome = manifest.chrome || {};
  const selectors = [chrome.header?.selector || 'header'];
  for (const spec of chrome.trailing || []) selectors.push(...(spec.selectors || []));
  if (!(chrome.trailing || []).length) selectors.push('footer');
  let out = html;
  for (const sel of [...new Set(selectors)]) {
    const main = findFirst(out, 'main');
    let node = null;
    if (!main) node = findFirst(out, sel);
    else {
      node = findFirst(out.slice(0, main.start), sel);
      if (!node) {
        const after = findFirst(out.slice(main.end), sel);
        if (after) node = { ...after, start: after.start + main.end, end: after.end + main.end };
      }
    }
    if (node) out = out.slice(0, node.start) + out.slice(node.end);
  }
  return out;
}

/** The open elements between `from` and `to` in html, outermost first. */
function openBetween(html, from, to) {
  const stack = [];
  const VOID = /^(area|base|br|col|embed|hr|img|input|link|meta|param|source|track|wbr)$/i;
  for (const m of html.slice(from, to).matchAll(/<!--[\s\S]*?-->|<(\/?)([A-Za-z][A-Za-z0-9-]*)((?:[^>"']|"[^"]*"|'[^']*')*)>/g)) {
    if (m[0].startsWith('<!--') || VOID.test(m[2]) || /\/\s*$/.test(m[3] || '')) continue;
    const tag = m[2].toLowerCase();
    if (m[1]) {
      const at = stack.lastIndexOf(tag);
      if (at !== -1) stack.length = at;
    } else stack.push(tag);
  }
  return stack;
}

/** For a descendant selector, the one `>` path (from its first step to its
 * last) that `accept` confirms on these pages; null when none or several. */
function suggestPath(htmls, selector, accept) {
  const parts = selector.trim().split(/\s+/);
  if (parts.length < 2 || !parts.every((part) => parseSelector(part))) return null;
  const [outer, inner] = [parts[0], parts[parts.length - 1]];
  const found = new Set();
  for (const html of htmls) {
    for (const top of findAll(html, outer)) {
      for (const node of findAll(top.outer, inner)) {
        const path = [outer, ...openBetween(top.outer, top.open.length, node.start), inner].join(' > ');
        if (!found.has(path) && accept(path)) found.add(path);
      }
    }
  }
  return found.size === 1 ? [...found][0] : null;
}

const routeOf = (file) => {
  const stem = String(file || '').replace(/\.html$/, '').replace(/(^|\/)index$/, '');
  return '/' + (stem ? stem.replace(/^\/+|\/+$/g, '') + '/' : '');
};

export function preflight(manifest, readPage) {
  const rows = [];
  const notes = [];
  let checked = false;
  const refuse = (name, field, file, what, lever) => rows.push({
    id: `manifest|${file ? routeOf(file) : '*'}|${name}.${field}`, check: 'manifest', kind: 'structural',
    route: file ? routeOf(file) : '*', width: null, field: `${name}.${field}`, what, lever: 'manifest', fix: lever,
  });
  const pageOf = new Map((manifest.pages || []).filter((p) => p && p.file).map((p) => [p.file, p]));
  const view = (file) => {
    const html = readPage(file);
    return html == null ? null : storedView(html, pageOf.get(file), manifest);
  };
  for (const [name, spec] of Object.entries(SECTIONS)) {
    const section = manifest[name];
    if (!section || typeof section !== 'object' || !section.present) continue;
    checked = true;
    const named = (field) => (typeof section[field] === 'string' && section[field].trim() ? section[field] : '');
    const listings = (manifest.pages || []).filter((p) => p && p.kind === spec.listing && p.file)
      .map((p) => ({ file: p.file, html: view(p.file) })).filter((p) => p.html != null);
    const detail = spec.pages(section) || [];
    const details = (Array.isArray(detail) ? detail : []).map((file) => ({ file, html: view(file) })).filter((p) => p.html != null);
    // The chrome stays in: a cart link lives in the header.
    const anyPages = [...pageOf.keys()].map((file) => ({ file, html: readPage(file) })).filter((p) => p.html != null);
    const card = named('cardSelector') || spec.card;
    // The article or product region (its own tag and classes excluded, as
    // make-theme demotes them): the named one, else <main>, else the page.
    const regionOf = (html) => {
      const sel = named(spec.main) && parseSelector(named(spec.main)) ? named(spec.main) : 'main';
      const main = findFirst(html, sel);
      return main ? main.outer.slice(main.open.length) : html;
    };

    // Every named field, inside the grammar.
    const broken = new Set();
    for (const field of Object.keys(FIELDS[name])) {
      const value = named(field);
      if (!value || parseSelector(value)) continue;
      broken.add(field);
      const where = FIELDS[name][field];
      const accept = where === 'listing' && field === 'cardContainer'
        ? (path) => listings.some((p) => { const f = findFirst(p.html, path); return f && cardsIn(f.outer, card) > 0; })
        : (path) => (where === 'region' ? details.map((p) => regionOf(p.html)) : where === 'any' ? anyPages.map((p) => p.html)
          : where === 'pages' ? details.map((p) => p.html) : listings.map((p) => p.html)).some((html) => findFirst(html, path));
      const pool = where === 'region' ? details.map((p) => regionOf(p.html)) : where === 'any' ? anyPages.map((p) => p.html)
        : where === 'pages' ? details.map((p) => p.html) : listings.map((p) => p.html);
      const path = suggestPath(pool, value, accept);
      refuse(name, field, null, `${name}.${field} "${value}" is outside the manifest's selector grammar: ${grammarRule(value)}. ` +
        'The service matches nothing with it.', path ? `Use "${path}" (checked on the built pages).` : 'Write it as a `>` path from an element it can name.');
    }

    // cardContainer: one grid on each listing, and it is the first match.
    for (const listing of listings) {
      if (!named('cardContainer')) {
        refuse(name, 'cardContainer', listing.file, `${name}.cardContainer is not set, so ${spec.stage} leaves ${listing.file} ` +
          `static (no ${spec.token} token) and make-zip refuses the theme.`, 'Name the element that holds the cards.');
        continue;
      }
      if (broken.has('cardContainer') || broken.has('cardSelector')) continue;
      const container = named('cardContainer');
      const all = findAll(listing.html, container);
      const first = findFirst(listing.html, container);
      const counts = all.map((node) => cardsIn(node.outer, card));
      const holding = counts.filter((n) => n > 0).length;
      const firstCards = first ? cardsIn(first.outer, card) : 0;
      const measured = `measured on ${listing.file}: ${all.length} match(es), ${holding} holding "${card}" cards, the first holding ${firstCards}`;
      if (!first) {
        refuse(name, 'cardContainer', listing.file, `${name}.cardContainer "${container}" matches nothing (${measured}).`,
          `Name the element that holds the "${card}" cards.`);
      } else if (!firstCards) {
        refuse(name, 'cardContainer', listing.file, `${name}.cardContainer "${container}": its first match holds no card, ` +
          `and ${spec.stage} wires only the first, so the listing stays static (${measured}).`,
          holding ? 'Name the card grid with a `>` path from an element only it sits under, or a class only it carries.'
            : `Name the element that holds the cards, and the card as ${name}.cardSelector.`);
      } else if (holding > 1) {
        refuse(name, 'cardContainer', listing.file, `${name}.cardContainer "${container}" names ${holding} card grids; ` +
          `${spec.stage} wires only the first and the others stay static (${measured}).`,
          'Name the one grid the listing is, with a `>` path or a class only it carries.');
      }
    }

    // The other fields: at least one element where each applies.
    const wired = listings.map((p) => (named('cardContainer') && !broken.has('cardContainer') ? findFirst(p.html, named('cardContainer')) : null))
      .filter(Boolean);
    for (const [field, where] of Object.entries(FIELDS[name])) {
      const value = named(field);
      if (!value || broken.has(field) || where === 'listing') continue;
      let pool;
      let place;
      if (where === 'card') {
        pool = wired.flatMap((grid) => findAll(grid.outer, card).map((node) => node.outer));
        place = 'the listing cards';
      } else if (where === 'pages') {
        pool = details.map((p) => p.html);
        place = `the ${name === 'blog' ? 'article' : 'product'} pages`;
      } else if (where === 'region') {
        pool = details.map((p) => regionOf(p.html));
        place = `the ${name === 'blog' ? 'article' : 'product'} pages${named(spec.main) ? `, inside ${spec.main}` : ''}`;
      } else {
        pool = anyPages.map((p) => p.html);
        place = 'any page';
      }
      if (!pool.length) continue;
      const hits = pool.filter((html) => findFirst(html, value)).length;
      if (!hits) {
        refuse(name, field, null, `${name}.${field} "${value}" matches nothing on ${place} (measured on ${pool.length}), ` +
          'so the service leaves it unplaced.', 'Name an element that is there, or leave the field out.');
      } else if (hits < pool.length && where !== 'any' && where !== 'card') {
        notes.push(`${name}.${field} "${value}" matches on ${hits} of ${pool.length} of ${place}.`);
      }
    }
  }
  return { refusals: rows.map((row) => `${row.what} ${row.fix}`), rows, notes, checked };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === resolve(process.argv[1])) {
  const arg = process.argv.slice(2).find((a) => a.startsWith('--manifest='));
  if (!arg) { console.error('usage: preflight-listings.mjs --manifest=conversion-manifest.json'); process.exit(2); }
  const manifestPath = resolve(arg.slice('--manifest='.length));
  const bytes = readFileSync(manifestPath);
  const manifest = JSON.parse(bytes.toString('utf8'));
  const dist = resolve(dirname(manifestPath), 'astro-project', 'dist');
  const readPage = (file) => {
    const path = resolve(dist, file);
    if (!path.startsWith(dist + sep) || !existsSync(path)) return null;
    return readFileSync(path, 'utf8');
  };
  const { refusals, rows, notes, checked } = preflight(manifest, readPage);
  if (checked) writeFileSync(resolve(dirname(manifestPath), 'preflight-listings.json'), `${JSON.stringify({
    schema: 'h2wp-preflight-listings/1', manifestSha256: createHash('sha256').update(bytes).digest('hex'),
    passed: !rows.length, rows, notes }, null, 2)}\n`);
  for (const note of notes) console.log(`note: ${note}`);
  if (refusals.length) {
    console.error('refusing: blog/shop selectors the service would leave unwired (checked on astro-project/dist; ' +
      'rows in preflight-listings.json):');
    for (const refusal of refusals) console.error(`  - ${refusal}`);
    process.exit(1);
  }
}
