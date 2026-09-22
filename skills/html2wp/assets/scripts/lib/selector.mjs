/**
 * The manifest's selector grammar, in one place.
 *
 * MANIFEST.md promises that blog.articleMain / articleBody and
 * shop.productMain / productBody take "tag", "tag.class" (several classes per
 * segment), `#id`, `:not(.class)` or a direct `>` path. make-theme honoured
 * that; build-posts and build-products each carried a private matcher that
 * parsed only `tag` / `tag.class`, so `main > article` matched nothing there
 * — every article was skipped, and the stage still said OK with 0 posts.
 * Two parsers for one documented grammar is how that happens, so the grammar
 * lives here and is imported.
 *
 * The engine is make-theme's own (parseSelectorSegment … findBySelector),
 * copied without change so the three generators agree by construction.
 */

const VOID = new Set([
  'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta',
  'param', 'source', 'track', 'wbr',
]);

// CSS escapes. A class name is written in a selector the way CSS requires —
// Tailwind's `lg:pt-6` is `.lg\:pt-6`, `w-1/2` is `.w-1\/2`, `gap-1.5` is
// `.gap-1\.5` — and a browser's querySelector, DevTools' "copy selector" and
// every other tool in this pipeline that runs in a browser take it in that
// form. The old class pattern had no backslash in it, so any escaped name
// parsed to null and the field it named was silently unplaced. `\X` is X,
// `\3A ` (1–6 hex digits, one optional trailing space) is that code point.
// The unescaped forms the grammar always accepted (`lg:pt-6`, `w-1/2`,
// `aspect-[4/5]`, `text-[11px]`) still parse to the same names.
function readEscape(sel, i) {
  const hex = /^[0-9a-fA-F]{1,6}/.exec(sel.slice(i + 1));
  if (hex) {
    let next = i + 1 + hex[0].length;
    if (/[ \t\n\r\f]/.test(sel[next] || '')) next += 1;
    const cp = parseInt(hex[0], 16);
    return { ch: cp && cp <= 0x10ffff ? String.fromCodePoint(cp) : '�', next };
  }
  if (i + 1 >= sel.length) return null;
  const cp = sel.codePointAt(i + 1);
  return { ch: String.fromCodePoint(cp), next: i + 1 + (cp > 0xffff ? 2 : 1) };
}

// One name (tag, id or class) starting at i. `lenient` is the class form: it
// also takes the unescaped Tailwind characters, and a `[…]` arbitrary value
// whole (so `w-[1.5rem]` keeps its dot) — but never an unescaped `:not(`,
// which starts the negation that follows a class.
function readName(sel, i, lenient) {
  let name = '';
  while (i < sel.length) {
    const c = sel[i];
    if (c === '\\') {
      const esc = readEscape(sel, i);
      if (!esc) return null;
      name += esc.ch; i = esc.next; continue;
    }
    if (/[\w-]/.test(c) || c.charCodeAt(0) > 0x7f) { name += c; i++; continue; }
    if (!lenient) break;
    if (c === ':' && sel.startsWith(':not(', i)) break;
    if (c === '[') {
      const close = sel.indexOf(']', i);
      if (close === -1) return null;
      name += sel.slice(i, close + 1); i = close + 1; continue;
    }
    if (/[:/%()\]]/.test(c)) { name += c; i++; continue; }
    break;
  }
  return { name, next: i };
}

export function parseSegment(selector) {
  const sel = String(selector);
  let i = 0;
  const tagMatch = /^[A-Za-z][\w-]*/.exec(sel);
  const tag = tagMatch ? tagMatch[0] : '';
  i = tag.length;
  let id = '';
  const classes = [];
  const notClasses = [];
  while (i < sel.length) {
    if (sel[i] === '#' && !id && classes.length === 0) {
      const r = readName(sel, i + 1, false);
      if (!r || !r.name) return null;
      id = r.name; i = r.next; continue;
    }
    if (sel[i] === '.') {
      const r = readName(sel, i + 1, true);
      if (!r || !r.name) return null;
      classes.push(r.name); i = r.next; continue;
    }
    if (sel.startsWith(':not(.', i)) {
      const r = readName(sel, i + 6, false);
      if (!r || !r.name || sel[r.next] !== ')') return null;
      notClasses.push(r.name); i = r.next + 1; continue;
    }
    return null;
  }
  if (!tag && !id && classes.length === 0) return null;
  return { tag: tag.toLowerCase(), id, classes, notClasses };
}

// The `>` path, split only on a combinator: an escaped `\>` and a `>` inside
// an arbitrary value (`[&>*]:mt-2`) belong to the class name.
export function splitPath(sel) {
  const parts = [];
  let cur = '', depth = 0;
  for (let i = 0; i < sel.length; i++) {
    const c = sel[i];
    if (c === '\\') { cur += c + (sel[i + 1] || ''); i++; continue; }
    if (c === '[') depth++;
    else if (c === ']' && depth) depth--;
    if (c === '>' && !depth) { parts.push(cur.trim()); cur = ''; continue; }
    cur += c;
  }
  parts.push(cur.trim());
  return parts;
}

/** `{ path: [segment, …] }`, or null when the selector is outside the grammar. */
export function parseSelector(sel) {
  const parts = splitPath(String(sel || '').trim());
  if (!parts.length || parts.some((part) => !part || /\s/.test(part.replace(/\\[0-9a-fA-F]{1,6}\s/g, '')))) return null;
  const path = parts.map(parseSegment);
  if (path.some((part) => !part)) return null;
  return { ...path[0], path };
}

function tree(html) {
  const roots = [];
  const stack = [];
  const tokens = /<!--[\s\S]*?-->|<(\/?)((?:[A-Za-z][A-Za-z0-9-]*))((?:[^>"']|"[^"]*"|'[^']*')*)>/g;
  for (const match of html.matchAll(tokens)) {
    if (match[0].startsWith('<!--')) continue;
    const closing = match[1] === '/';
    const tag = (match[2] || '').toLowerCase();
    if (closing) {
      let at = stack.length - 1;
      while (at >= 0 && stack[at].tag !== tag) at--;
      if (at < 0) continue;
      const end = match.index + match[0].length;
      for (let i = stack.length - 1; i >= at; i--) {
        stack[i].end = end;
        if (i === at) stack[i].closeStart = match.index;
      }
      stack.splice(at);
      continue;
    }
    const node = { tag, start: match.index, openEnd: match.index + match[0].length,
      end: match.index + match[0].length, closeStart: null, children: [] };
    const parent = stack[stack.length - 1];
    if (parent) parent.children.push(node); else roots.push(node);
    if (!/\/\s*>$/.test(match[0]) && !VOID.has(tag)) stack.push(node);
  }
  for (const node of stack) node.end = html.length;
  return roots;
}

function attribute(openTag, name) {
  // [\s\S], not `.`: a class list wrapped across lines is still one value,
  // and missing it drops every card of a hand-formatted listing. Not preceded
  // by [\w-], so `data-class=` is not read as `class=`.
  const match = openTag.match(new RegExp(`(?<![\\w-])${name}\\s*=\\s*(["'])([\\s\\S]*?)\\1`, 'i'));
  return match ? match[2] : '';
}

function matches(html, node, segment) {
  if (segment.tag && node.tag !== segment.tag) return false;
  const openTag = html.slice(node.start, node.openEnd);
  if (segment.id && attribute(openTag, 'id') !== segment.id) return false;
  const classes = attribute(openTag, 'class').split(/\s+/).filter(Boolean);
  return segment.classes.every((cls) => classes.includes(cls)) &&
    segment.notClasses.every((cls) => !classes.includes(cls));
}

function flatten(roots) {
  const out = [];
  const visit = (node) => { out.push(node); node.children.forEach(visit); };
  roots.forEach(visit);
  return out;
}

/**
 * The first element (in document order, starting at or after `from`) that the
 * selector names, as `{ start, end, open, outer }` — the shape the generators'
 * own matchers returned, so a caller switches over without other changes.
 * null when nothing matches or the selector is outside the grammar.
 */
export function findFirst(html, sel, from = 0) {
  const parsed = parseSelector(sel);
  if (!parsed) return null;
  for (const first of flatten(tree(html))) {
    if (first.start < from || !matches(html, first, parsed.path[0])) continue;
    let current = first;
    for (const segment of parsed.path.slice(1)) {
      current = current.children.find((child) => matches(html, child, segment));
      if (!current) break;
    }
    if (current) {
      return { start: current.start, end: current.end,
        open: html.slice(current.start, current.openEnd), outer: html.slice(current.start, current.end) };
    }
  }
  return null;
}

/**
 * EVERY element the selector names, in document order — each step of a `>`
 * path takes all matching children, not only the first (findFirst's walk).
 * Same result shape as findFirst.
 */
export function findAll(html, sel) {
  const parsed = parseSelector(sel);
  if (!parsed) return [];
  let level = flatten(tree(html)).filter((node) => matches(html, node, parsed.path[0]));
  for (const segment of parsed.path.slice(1)) {
    level = level.flatMap((node) => node.children.filter((child) => matches(html, child, segment)));
  }
  const seen = new Set();
  return level
    .filter((node) => (seen.has(node.start) ? false : seen.add(node.start)))
    .sort((a, b) => a.start - b.start)
    .map((node) => ({ start: node.start, end: node.end,
      open: html.slice(node.start, node.openEnd), outer: html.slice(node.start, node.end) }));
}
