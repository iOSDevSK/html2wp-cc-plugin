// Nav zone stamping — the ONE implementation make-theme.mjs (theme parts +
// front-page pattern) and dist-to-bundle.mjs (stored page sources) share.
//
// The problem this solves: a menu zone must be addressable by a selector
// that matches EXACTLY ONE element — the plugin's render swap, the bridge's
// el.closest() and the gates all key on it — and real sites frequently
// cannot provide one. A Tailwind-built site's nav classes are utilities
// ("flex", shared by half the page) or not valid CSS class selectors at all
// ("group/navigation-menu", "lg:flex"). Hoping the site's class discipline
// cooperates is exactly the kind of assumption this pipeline exists to
// remove — so the converter, which OWNS the markup it emits, stamps each
// located group with a generated `data-ve-nav="{n}"` attribute, and the
// theme contract addresses zones by that: unique by construction, valid
// CSS, trivially matchable in PHP. (A hand-written theme like the
// reference's may still declare class selectors; both forms work.)
//
// The attribute is added to GENERATED artifacts only (theme parts, the
// front-page pattern, bundle sources) — never to the Astro build, so gate
// A/A2's byte-fidelity comparison against the original is untouched. A
// data attribute renders no pixels, so gate B is untouched too.
//
// Location strategy, per entry, in order:
//   1. the manifest entry's own selector, when it matches exactly one
//      element in this document — the nice-site case;
//   2. the entry's LINK SEQUENCE: elements of the entry's tag whose
//      contained <a> hrefs (permalink-normalized on both sides) equal the
//      entry's links. Among unstamped matches the SMALLEST wins — a
//      wrapper around the real link row contains the same links, and
//      stamping the wrapper would make the zone bigger than the menu. Two
//      same-links elements (a desktop nav and its mobile twin) resolve by
//      entry order: each entry consumes one unstamped match.
//   3. a PLACEHOLDER menu (every href "#" or empty: a template's footer
//      column) has no targets to compare, so its words are its identity:
//      steps 1 and 2 match it by its label sequence. When no element carries
//      those labels, the first unstamped element of the selector's own shape
//      (same tag and classes, the same number of links, all placeholders)
//      is taken — by position, entry order consuming them in turn — and
//      the result says so (`by: 'position'`).
//
// Hrefs compare by the page they name, not by how they are spelled:
// "about.html", "/about", "/about/" and "about/index.html" are one target,
// as the permalink "/about/" the converter rewrote them to is (sameTarget).
//
// Idempotent: an element already stamped with this entry's number is left
// alone; an element stamped with another number is never a candidate.

import { parseSegment } from './selector.mjs';

const TAG_ATTRS = `(?:[^>"']|"[^"]*"|'[^']*')*`;
const SEP = '\n'; // sequence-join separator no href can contain

/** Normalize an href for sequence comparison: permalink-map internal page
 * links (the caller supplies the same mapping its own rewriter used),
 * strip ./ and ../ prefixes off the rest. */
export function normalizeHref(href, permalinkOf) {
  const mapped = permalinkOf ? permalinkOf(href) : null;
  if (mapped) return mapped;
  return String(href || '').replace(/^(\.\.?\/)+/, '');
}

// A link that goes nowhere yet: "#", "#!", "" or javascript:.
const placeholder = (href) => !href || /^#!?$/.test(href) || /^javascript:/i.test(href);

/** The page a link names, however it is spelled: "about.html", "/about",
 * "/about/", "about/index.html" and "../about" all read "about", the front
 * page ("/", "index.html") reads "/", and every placeholder reads "#". Anything
 * else — an external URL, mailto:, a fragment of this page — is compared as
 * written. The fragment or query of a page link stays part of its identity. */
export function sameTarget(href) {
  const raw = String(href || '');
  if (placeholder(raw)) return '#';
  if (raw.startsWith('#') || /^[a-z][a-z0-9+.-]*:/i.test(raw) || raw.startsWith('//') || raw.includes('__CLARA_')) return raw;
  const at = raw.search(/[#?]/);
  const path = at < 0 ? raw : raw.slice(0, at);
  const tail = at < 0 ? '' : raw.slice(at);
  const page = path.replace(/^(\.\.?\/)+/, '').replace(/^\/+/, '').replace(/\/+$/, '')
    .replace(/(^|\/)index\.html?$/i, '').replace(/\.html?$/i, '').replace(/\/+$/, '');
  // The front page keeps its slash, so "index.html#about" — another page's
  // link to a section of the front page — never reads as a bare "#about".
  return (page || '/') + tail;
}

function linksOf(html) {
  return [...html.matchAll(new RegExp(`<a\\b(${TAG_ATTRS})>`, 'gi'))]
    .map((m) => (m[1].match(/\bhref=["']([^"']*)["']/i) || [undefined, ''])[1]);
}

function classOf(tagHtml) {
  const d = tagHtml.match(/\bclass="([^"]*)"/i);
  if (d) return d[1];
  const s = tagHtml.match(/\bclass='([^']*)'/i);
  return s ? s[1] : '';
}

function* eachTag(html, tag) {
  const openRe = new RegExp(`<${tag}\\b${TAG_ATTRS}>`, 'gi');
  let m;
  while ((m = openRe.exec(html))) {
    const start = m.index;
    const step = new RegExp(`<${tag}\\b${TAG_ATTRS}>|</${tag}>`, 'gi');
    step.lastIndex = start;
    let depth = 0;
    let hit;
    let end = html.length;
    while ((hit = step.exec(html))) {
      depth += hit[0][1] === '/' ? -1 : 1;
      if (depth === 0) { end = hit.index + hit[0].length; break; }
    }
    yield { open: m[0], start, end, outer: html.slice(start, end) };
  }
}

/**
 * @param {string} html         Document/fragment to stamp (links already
 *                              rewritten to permalink form by the caller).
 * @param {Array}  entries      [{ n, selector, hrefs }] — n is the 1-based
 *                              manifest index; hrefs already normalized
 *                              via normalizeHref by the caller.
 * @returns {{ html: string, stamped: number[] }}
 */
/**
 * Locate ONE nav entry's zone in a document, by the strategy documented above.
 * Exported so anything that needs the zone's own markup — not just the stamp —
 * uses this same implementation. Two derivations of "where is this menu" would
 * drift, and a zone located differently by two callers is exactly the class of
 * bug the stamping exists to end.
 *
 * @param {string} html
 * @param {{selector?: string, hrefs?: string[]}} entry
 * @returns {{open: string, start: number, end: number, outer: string}|null}
 */
// Link text for comparison: tags stripped, the common entities decoded,
// whitespace collapsed, case folded.
const labelOf = (html) => String(html || '').replace(/<[^>]*>/g, ' ').replace(/&amp;/g, '&').replace(/&nbsp;/g, ' ')
  .replace(/&#0*39;|&apos;/g, "'").replace(/\s+/g, ' ').trim().toLowerCase();

function linkPairsOf(html) {
  return [...html.matchAll(new RegExp(`<a\\b(${TAG_ATTRS})>([\\s\\S]*?)<\\/a\\s*>`, 'gi'))]
    .map((m) => ({ href: normalizeHref((m[1].match(/\bhref=["']([^"']*)["']/i) || [undefined, ''])[1], null), text: labelOf(m[2]) }));
}

// Is this element the entry's menu? Rendering a menu into a zone REPLACES the
// zone's links with the menu's items, so anything the element carries that
// the menu does not is lost. Measured on one site, each a different way:
// a home-page variant's own nav of four #anchors (menu rendered into the
// hero), an eight-link footer nav sharing four links with a five-link menu,
// a variant footer whose links point at the same pages under its own labels
// ("Case Studies" for Portfolio), and one whose list adds an item the menu
// lacks. So: every real link of the element must be a link of the entry,
// under the entry's label when the entry states labels, and the two must
// share at least half of their union. A current-page link written as "#" or
// "" is the one allowed stranger. An entry without links, or an empty zone
// (a placeholder a script fills), keeps the old behaviour; an element with
// words and no links is a list of something else (the article part's hidden
// typography specimen).
function sharesLinks(el, entry) {
  const hrefs = (entry.hrefs || []).map(sameTarget);
  if (hrefs.length < 2) return true;
  const inner = el.outer.slice(el.open.length);
  const own = linkPairsOf(inner).map((l) => ({ ...l, href: sameTarget(l.href) }));
  if (!own.length) return !inner.replace(/<[^>]*>/g, '').trim();
  const labels = Array.isArray(entry.texts) && entry.texts.length === hrefs.length ? entry.texts.map(labelOf) : null;
  // A placeholder menu is known by its words alone, in order, and only on
  // links that are placeholders too: its "#" items rendered over a list with
  // real targets would take those targets away.
  if (hrefs.every((h) => h === '#')) {
    return !!labels && own.length === labels.length && own.every((l, i) => l.href === '#' && l.text === labels[i]);
  }
  const want = new Map(hrefs.map((h, i) => [h, labels ? labels[i] : null]));
  const real = own.filter((l) => l.href !== '#');
  if (real.some((l) => !want.has(l.href) || (want.get(l.href) !== null && want.get(l.href) !== l.text))) return false;
  const mine = new Set(real.map((l) => l.href));
  const shared = [...mine].filter((h) => want.has(h)).length;
  return shared * 2 >= new Set([...mine, ...want.keys()]).size;
}

export function findNavZone(html, entry) {
  // The manifest's selector grammar (lib/selector.mjs): EVERY class of
  // "div.flex-col.gap-3" is required, and CSS-escaped names (`lg\:flex`)
  // read as the class they spell. Splitting on '.' kept only the first class,
  // so "div.flex-col.gap-3" matched any div with flex-col.
  const seg = parseSegment(String(entry.selector || 'nav')) || { tag: 'nav', classes: [] };
  const tag = seg.tag || 'nav';

  // 1) the declared selector, when it is unambiguous here. A bare element
  // selector is a supported manifest form too: Radiant's only menu is a
  // plain <nav>, and the old class-only branch accidentally sent that valid
  // selector straight to the fallback.
  //
  // Unambiguous is not enough on its own: the element must also be this
  // menu (sharesLinks). A page's stored source has no footer,
  // so a footer column's selector can match exactly one OTHER element there —
  // measured: the page wrapper `div.min-h-screen.flex.flex-col` was stamped as
  // the footer menu's zone on every page, which hands the whole page to the
  // menu renderer. A zone without a single one of its links is not the menu.
  const matches = [];
  for (const el of eachTag(html, tag)) {
    const classes = classOf(el.open).split(/\s+/);
    const hasClass = seg.classes.every((c) => classes.includes(c));
    if (hasClass && !/\bdata-ve-nav=/.test(el.open)) matches.push(el);
  }
  // ...and when it plausibly IS this menu (sharesLinks, above). That also
  // refuses the case the selector alone let through: in a page's stored
  // source (no footer) the page wrapper was the one match for a footer
  // column's selector, and it holds none of the menu's links.
  // Several elements wear the selector (three footer columns of one class)
  // and only one of them is this menu: that one.
  const fit = matches.filter((el) => sharesLinks(el, entry));
  if (fit.length === 1) return { ...fit[0], by: 'selector' };

  // 2) link-sequence match; smallest unstamped match wins (see header)
  const hrefs = (entry.hrefs || []).map(sameTarget);
  if (hrefs.length >= 2) {
    const want = hrefs.join(SEP);
    let target = null;
    for (const el of eachTag(html, tag)) {
      if (/\bdata-ve-nav=/.test(el.open)) continue;
      const inner = el.outer.slice(el.open.length);
      // The same targets under the page's own labels are still not this menu.
      if (linksOf(inner).map(sameTarget).join(SEP) === want && sharesLinks(el, entry)) {
        if (!target || el.outer.length < target.outer.length) target = el;
      }
    }
    if (target) return { ...target, by: 'sequence' };
  }

  // 3) a placeholder menu whose words match no element: the first unstamped
  // element of the selector's own shape, by position (see header).
  if (hrefs.length >= 2 && hrefs.every((h) => h === '#')) {
    const shaped = matches.find((el) => {
      const own = linksOf(el.outer.slice(el.open.length));
      return own.length === hrefs.length && own.every((h) => sameTarget(h) === '#');
    });
    if (shaped) return { ...shaped, by: 'position' };
  }
  return null;
}

/**
 * The hrefs (normalized, no permalink map) of a zone's links that open in a
 * new tab — `target="_blank"` on the source anchor. The manifest records a
 * nav link's text and href only, and a managed zone renders from its menu,
 * so a menu item that does not carry the target drops it from the page.
 *
 * @param {string} zoneHtml The zone's outer markup (findNavZone().outer).
 * @returns {Set<string>}
 */
export function newTabHrefs(zoneHtml) {
  const out = new Set();
  for (const m of String(zoneHtml || '').matchAll(new RegExp(`<a\\b(${TAG_ATTRS})>`, 'gi'))) {
    const href = (m[1].match(/\bhref=(?:"([^"]*)"|'([^']*)')/i) || []);
    const target = (m[1].match(/\btarget=(?:"([^"]*)"|'([^']*)'|([^\s>]+))/i) || []);
    const h = href[1] ?? href[2];
    const t = target[1] ?? target[2] ?? target[3] ?? '';
    if (h !== undefined && /^_blank$/i.test(t.trim())) out.add(normalizeHref(h, null));
  }
  return out;
}

/**
 * Close every </a> in a stamped zone WITHOUT whitespace before the '>'.
 *
 * A zone is the one region the plugin re-renders from the menu on every
 * request, and its renderer finds links with a non-greedy match up to a
 * LITERAL '</a>'. A source formatted by Prettier writes '>Text</a' + newline
 * + '>' to keep an inline element from introducing a space, which is
 * identical HTML — the tokenizer ignores whitespace inside an end tag — and
 * invisible to that match. The scan then runs past the real closing tag to
 * the next literal one, so several links read as ONE, the count no longer
 * equals the menu's, and the regenerate path rebuilds the whole zone from
 * the first link as a template. Verified live on ai-starter-kit's front page,
 * whose header nav is written that way: the plugin rendered 11 nested
 * dropdown wrappers where the design has 2, dropped every icon span, and
 * collapsed the nav from 367px wide to 184px — while the pixel gate scored
 * the page 0.001, because a mangled nav row is nothing on a 9000px page.
 *
 * Chrome captured through a browser is already normalised; a page's own
 * stored source and the front-page pattern keep the file's bytes, which is
 * where this bites. Rewriting only the end tag's inner whitespace changes no
 * rendered pixel and only ever runs inside a zone this function just stamped.
 *
 * @param {string} html
 * @returns {string}
 */
function closeAnchorsTightly(html) {
  return html.replace(/<\/a\s+>/gi, '</a>');
}

export function stampNavZones(html, entries) {
  const stamped = [];
  const positional = [];
  for (const entry of entries) {
    if (new RegExp(`\\bdata-ve-nav="${entry.n}"`).test(html)) {
      stamped.push(entry.n); // already stamped (re-run) — idempotent
      continue;
    }
    const target = findNavZone(html, entry);
    if (!target) continue;
    if (target.by === 'position') positional.push(entry.n);
    const openStamped = target.open.replace(/^<([a-zA-Z0-9-]+)/, `<$1 data-ve-nav="${entry.n}"`);
    const rest = closeAnchorsTightly(
      html.slice(target.start + target.open.length, target.end),
    );
    html = html.slice(0, target.start) + openStamped + rest + html.slice(target.end);
    stamped.push(entry.n);
  }
  return { html, stamped, positional };
}
