// Collection normalisation — the ONE implementation make-theme.mjs (theme
// parts + front-page pattern) and dist-to-bundle.mjs (stored page sources)
// share, alongside nav-stamp's.
//
// The problem this solves: a repeated section a REVIEWER has confirmed is one
// editable list, which the editor's own congruence rules refuse because one
// member is an authored design variant. A pricing table is the ordinary case —
// `tier reveal`, `tier feature reveal`, `tier reveal`, where the middle card is
// the highlighted package. The class is real: `.tier.feature` paints it dark,
// so removing it would change the design, and the design is the one thing this
// pipeline may not touch.
//
// The editor already has the hook. bridge.js reads `data-cve-class` in
// preference to the live className when it compares members — a pristine-class
// snapshot it takes ONCE and never overwrites (`if (!hasAttribute(...))`),
// there so a scroll-reveal script adding `in` to whichever cards the visitor
// scrolled past cannot make identical cards look different. Writing that
// snapshot ourselves, with the classes every member SHARES, makes the group
// congruent for comparison while every member keeps its own `class` attribute
// and renders exactly as designed. Measured on the case above: the group goes
// from 2 congruent members out of 3 (so no collection at all) to 3 members and
// 3 editable slots, with the highlighted card still dark.
//
// Stamped into GENERATED artifacts only — theme parts, the front-page pattern,
// stored page sources — never into the Astro build. Gate A2 is a STRUCTURAL
// comparison against the original and reads an added data attribute exactly as
// it reads a dropped one: measured, it fails the gate at the character offset
// of the stamp. nav-stamp's own header says the same thing about `data-ve-nav`.
//
// A group that cannot be located, or whose members are too far apart to call
// one design, is left alone. Unlike a menu — whose zone, unstamped, is a dead
// feature — an unstamped collection is simply a panel the editor does not
// offer, which is where the conversion already was.

const TAG_ATTRS = `(?:[^>"']|"[^"]*"|'[^']*')*`;

/** How many classes a member may carry beyond the shared set. */
const MAX_VARIANT_CLASSES = 2;

function classOf(tagHtml) {
  const d = tagHtml.match(/\bclass="([^"]*)"/i);
  if (d) return d[1];
  const s = tagHtml.match(/\bclass='([^']*)'/i);
  return s ? s[1] : '';
}

const classSet = (tagHtml) => new Set(classOf(tagHtml).split(/\s+/).filter(Boolean));

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

/** Elements of `tag` that are not nested inside an earlier one — the siblings. */
function topLevel(html, tag) {
  const out = [];
  let consumed = 0;
  for (const el of eachTag(html, tag)) {
    if (el.start < consumed) continue;
    out.push(el);
    consumed = el.end;
  }
  return out;
}

/**
 * The one parent this group names, or null when that is not certain.
 *
 * Identity is the loose probe's own: the parent's tag and class set, the
 * members' tag, and how many of them there are. Two parents answering to it
 * is not a tie to break — it is a group we cannot say we located, and a stamp
 * on the wrong section is worse than no stamp.
 */
export function findCollectionParent(html, group) {
  const want = new Set(String(group.parentClasses || '').split(/\s+/).filter(Boolean));
  const matches = [];

  for (const parent of eachTag(html, group.parentTag)) {
    const have = classSet(parent.open);
    if (have.size !== want.size || [...want].some((c) => !have.has(c))) continue;
    const inner = parent.outer.slice(parent.open.length, -(`</${group.parentTag}>`.length));
    const members = topLevel(inner, group.tag);
    if (members.length !== group.count) continue;
    matches.push({ parent, inner, members, innerAt: parent.start + parent.open.length });
  }

  return matches.length === 1 ? matches[0] : null;
}

/**
 * The classes every member shares, or null when they are too far apart.
 *
 * "Too far apart" is deliberately tight: a member may carry a couple of
 * classes the others do not — `feature` on the middle package, `is-last` on
 * the final card — and beyond that the reviewer's "these are one list" is
 * describing something this cannot express by hiding classes.
 */
export function sharedClasses(members) {
  const sets = members.map((m) => classSet(m.open));
  if (!sets.length) return null;

  const shared = [...sets[0]].filter((c) => sets.every((s) => s.has(c)));
  // An EMPTY shared set is not the same thing as "too far apart", and
  // conflating the two cost a real site both of its FAQ lists. The ordinary
  // accordion is `<details>` with no class at all, until the export freezes
  // one of them open and that one alone carries `is-open` — so the shared set
  // is empty while the members are as identical as markup gets. What decides
  // is the VARIANCE below: nobody may carry more than a couple of classes the
  // others lack, and an empty snapshot then says exactly the true thing, that
  // these members' classes are not what distinguishes them. bridge.js reads
  // the attribute's presence rather than its content, so an empty value makes
  // every member compare equal.
  if (sets.some((s) => s.size - shared.length > MAX_VARIANT_CLASSES)) return null;

  return shared.sort().join(' ');
}

/**
 * @param {string} html    Document or fragment to stamp.
 * @param {Array}  groups  [{ parentTag, parentClasses, tag, count }] — the
 *                         groups a reviewer confirmed are real, as the
 *                         collections report recorded them.
 * @returns {{ html: string, stamped: Array, skipped: Array }}
 */
export function stampCollections(html, groups) {
  const stamped = [];
  const skipped = [];

  for (const group of groups || []) {
    const found = findCollectionParent(html, group);
    if (!found) {
      skipped.push({ group, why: 'not located here' });
      continue;
    }

    const shared = sharedClasses(found.members);
    if (shared === null) {
      skipped.push({ group, why: 'members share too little to call one design' });
      continue;
    }

    // Already done on a re-run: leave every byte alone.
    if (found.members.every((m) => /\bdata-cve-class=/.test(m.open))) {
      stamped.push({ group, shared, members: found.members.length });
      continue;
    }

    // Right to left, so an earlier member's offsets survive a later edit.
    let inner = found.inner;
    for (const member of [...found.members].reverse()) {
      if (/\bdata-cve-class=/.test(member.open)) continue;
      const open = member.open.replace(/^<([a-zA-Z0-9-]+)/, `<$1 data-cve-class="${shared}"`);
      inner = inner.slice(0, member.start) + open + inner.slice(member.start + member.open.length);
    }

    html = html.slice(0, found.innerAt) + inner + html.slice(found.innerAt + found.inner.length);
    stamped.push({ group, shared, members: found.members.length });
  }

  return { html, stamped, skipped };
}
