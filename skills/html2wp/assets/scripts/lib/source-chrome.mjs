/** A conservative, measured header selector for sites without <header>.
 * This is coverage evidence, not permission to canonicalize shared parts.
 */
import { findAll, findFirst } from './selector.mjs';

function attribute(open, name) {
  return (open.match(new RegExp(`\\s${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)')`, 'i')) || []).slice(1).find(v => v !== undefined) || '';
}

export function headerCandidate(html) {
  const original = html;
  // The lightweight selector engine is not a browser HTML parser. Do not let
  // embedded example strings or inert templates provide fallback evidence.
  html = html.replace(/<!--[\s\S]*?(?:-->|$)/g, m => ' '.repeat(m.length));
  html = html.replace(/<(script|style|textarea|title|xmp|iframe|noembed|noframes|noscript)\b(?:[^>"']|"[^"]*"|'[^']*')*>[\s\S]*?<\/\1\s*>/gi,
    m => ' '.repeat(m.length));
  // Nested templates or unterminated raw-text elements need a full parser;
  // abstain rather than infer a selector from their contents.
  if (/<(?:template|script|style|textarea|title|xmp|iframe|noembed|noframes|noscript)\b/i.test(html)) return null;
  if (findFirst(html, 'header')) return null;
  const main = findFirst(html, 'main');
  const nav = findFirst(html, 'nav');
  if (!main || !nav || nav.end > main.start) return null;
  const candidates = ['div', 'section', 'aside'].flatMap(tag => findAll(html, tag).map(node => ({...node, tag})))
    .filter(node => node.start <= nav.start && node.end >= nav.end && node.end <= main.start)
    .filter(node => {
      const classes = attribute(node.open, 'class').split(/\s+/);
      return attribute(node.open, 'role') === 'banner'
        || classes.some(c => /^(?:site[-_]?header|masthead|header|navbar)$/i.test(c))
        || (classes.includes('sticky') && classes.includes('top-0'));
    }).sort((a,b) => a.start - b.start || b.end - a.end);
  if (!candidates.length) return null;
  const node = candidates[0];
  const id = attribute(node.open, 'id');
  const classes = attribute(node.open, 'class').split(/\s+/).filter(c => /^[A-Za-z_][\w-]*$/.test(c));
  const selectors = /^[A-Za-z_][\w-]*$/.test(id) ? ['#' + id] : [];
  selectors.push(...classes.map(c => node.tag + '.' + c));
  if (classes.length) selectors.push(node.tag + classes.map(c => '.' + c).join(''));
  for (const selector of selectors) {
    const found = findAll(html, selector);
    // A2 consumes the original document with the same lightweight engine.
    // A selector must address the real node there too, not a raw-text example.
    const originalFound = findAll(original, selector);
    if ([found, originalFound].every(matches => matches.length === 1
      && matches[0].start === node.start && matches[0].end === node.end)) {
      return {selector, reason: 'unique navigation wrapper before main', containsNavigation: true};
    }
  }
  return null;
}
