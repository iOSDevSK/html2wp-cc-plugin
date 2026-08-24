// The decorative lead immediately before a shared header can belong to the
// source page's main, while make-theme promotes it into the header part. Stage
// 4 must remove that exact same element from the stored page source or the
// rendered page paints it twice.

const TAG_ATTRS = `(?:[^>"']|"[^"]*"|'[^']*')*`;
const VOID_TAGS = new Set([
  'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta',
  'param', 'source', 'track', 'wbr',
]);

function classValue(tagHtml) {
  const match = tagHtml.match(/\bclass\s*=\s*(["'])(.*?)\1/i);
  return match ? match[2] : '';
}

function firstTag(html, tag, from = 0) {
  const open = new RegExp(`<${tag}\\b${TAG_ATTRS}>`, 'ig');
  open.lastIndex = from;
  const found = open.exec(html);
  if (!found) return null;

  const tags = new RegExp(`<${tag}\\b${TAG_ATTRS}>|</${tag}>`, 'gi');
  tags.lastIndex = found.index;
  let depth = 0;
  let match;
  while ((match = tags.exec(html))) {
    depth += match[0][1] === '/' ? -1 : 1;
    if (depth === 0) {
      return { outer: html.slice(found.index, match.index + match[0].length), start: found.index };
    }
  }
  return { outer: html.slice(found.index), start: found.index };
}

function directChildrenShape(inner) {
  const tagRe = new RegExp(`<(/?)([a-z][\\w:-]*)\\b${TAG_ATTRS}>`, 'gi');
  let depth = 0;
  let last = 0;
  let hasText = false;
  const children = [];
  let match;
  while ((match = tagRe.exec(inner))) {
    const text = inner.slice(last, match.index).replace(/<!--[\s\S]*?-->/g, '').trim();
    if (depth === 0 && text) hasText = true;
    if (match[1]) {
      depth = Math.max(0, depth - 1);
    } else {
      if (depth === 0) children.push(match[2].toLowerCase());
      if (!VOID_TAGS.has(match[2].toLowerCase()) && !/\/\s*>$/.test(match[0])) depth += 1;
    }
    last = match.index + match[0].length;
  }
  if (depth === 0 && inner.slice(last).replace(/<!--[\s\S]*?-->/g, '').trim()) hasText = true;
  return { children, hasText };
}

/**
 * Return the exact source span that make-theme promotes before a shared
 * header. `targetStart` is supplied by each stage's manifest-selector
 * matcher; the structural decision after that point is shared here.
 */
export function sourceLeadForTarget(pageHtml, targetStart) {
  if (!Number.isInteger(targetStart) || targetStart < 0) return null;

  const tagRe = new RegExp(`<(/?)([a-z][\\w:-]*)\\b${TAG_ATTRS}>`, 'gi');
  const stack = [];
  let match;
  while ((match = tagRe.exec(pageHtml)) && match.index < targetStart) {
    const tag = match[2].toLowerCase();
    if (match[1]) {
      for (let i = stack.length - 1; i >= 0; i--) {
        if (stack[i].tag === tag) {
          stack.length = i;
          break;
        }
      }
    } else if (!VOID_TAGS.has(tag) && !/\/\s*>$/.test(match[0])) {
      stack.push({ tag, start: match.index });
    }
  }

  const mainIndex = stack.map((node) => node.tag).lastIndexOf('main');
  if (mainIndex < 0) return null;
  const chain = stack.slice(mainIndex + 1);
  if (!chain.length) return null;

  const main = firstTag(pageHtml, 'main');
  const mainOpen = main?.outer.match(new RegExp(`^<main\\b${TAG_ATTRS}>`, 'i'))?.[0];
  if (!main || !mainOpen) return null;

  const prefixRaw = pageHtml.slice(main.start + mainOpen.length, chain[0].start);
  const prefix = prefixRaw.trim();
  const lead = firstTag(prefix, 'div');
  if (!lead || lead.outer.trim() !== prefix) return null;

  const leadOpen = lead.outer.match(new RegExp(`^<div\\b${TAG_ATTRS}>`, 'i'))?.[0];
  const leadClose = /<\/div>$/i.test(lead.outer) ? '</div>' : '';
  const leadInner = leadOpen ? lead.outer.slice(leadOpen.length, lead.outer.length - leadClose.length) : '';
  const child = firstTag(leadInner, 'div');
  const childOpen = child?.outer.match(new RegExp(`^<div\\b${TAG_ATTRS}>`, 'i'))?.[0];
  const childClass = childOpen ? classValue(childOpen).split(/\s+/) : [];
  const childShape = directChildrenShape(leadInner);
  if (!child || childShape.children.length !== 1 || childShape.hasText ||
      !childClass.includes('absolute') || /\b(?:src|href|poster)=/i.test(lead.outer)) {
    return null;
  }

  const leading = prefixRaw.search(/\S/);
  const start = main.start + mainOpen.length + (leading < 0 ? 0 : leading);
  const markup = lead.outer.trim();
  return { markup, start, end: start + markup.length };
}
