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
//
// Idempotent: an element already stamped with this entry's number is left
// alone; an element stamped with another number is never a candidate.

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
export function findNavZone(html, entry) {
  const [tag, cls] = String(entry.selector || 'nav').split('.');
  let target = null;

  // 1) the declared selector, when it is unambiguous here. A bare element
  // selector is a supported manifest form too: Radiant's only menu is a
  // plain <nav>, and the old class-only branch accidentally sent that valid
  // selector straight to the fallback.
  const matches = [];
  for (const el of eachTag(html, tag)) {
    const hasClass = !cls || classOf(el.open).split(/\s+/).includes(cls);
    if (hasClass && !/\bdata-ve-nav=/.test(el.open)) matches.push(el);
  }
  if (matches.length === 1) target = matches[0];

  // 2) link-sequence match; smallest unstamped match wins (see header)
  if (!target && (entry.hrefs || []).length >= 2) {
    const want = entry.hrefs.join(SEP);
    for (const el of eachTag(html, tag)) {
      if (/\bdata-ve-nav=/.test(el.open)) continue;
      const inner = el.outer.slice(el.open.length);
      if (linksOf(inner).map((h) => normalizeHref(h, null)).join(SEP) === want) {
        if (!target || el.outer.length < target.outer.length) target = el;
      }
    }
  }
  return target;
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
  for (const entry of entries) {
    if (new RegExp(`\\bdata-ve-nav="${entry.n}"`).test(html)) {
      stamped.push(entry.n); // already stamped (re-run) — idempotent
      continue;
    }
    const target = findNavZone(html, entry);
    if (!target) continue;
    const openStamped = target.open.replace(/^<([a-zA-Z0-9-]+)/, `<$1 data-ve-nav="${entry.n}"`);
    const rest = closeAnchorsTightly(
      html.slice(target.start + target.open.length, target.end),
    );
    html = html.slice(0, target.start) + openStamped + rest + html.slice(target.end);
    stamped.push(entry.n);
  }
  return { html, stamped };
}
