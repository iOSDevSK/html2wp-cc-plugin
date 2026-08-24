// Proof that two files are the SAME PAGE emitted at two depths.
//
// A static exporter routinely writes a page twice — about.html for the flat
// URL and about/index.html for the directory one. The two files are not
// byte-identical, because each carries relative references correct at its own
// depth (`assets/x.css` vs `../assets/x.css`) and some exporters add a
// `<base href="">` to the nested copy. Strip exactly those two differences and
// what remains must match, or the pages are genuinely different and nothing
// here may merge them.
//
// Measured on the eight colliding sites in the queue: 60 pairs, of which 46
// prove equal and 14 do not. The 14 are not near-misses — dexler-nextjs ships
// a hand-written site AND a minified export in one folder, so its about.html
// is 9.5 kB over 168 lines titled "About Us | Dexler" while about/index.html
// is 42.6 kB over 6 lines titled "About", with 117 differing lines of text.
//
// The canary is not decoration. The first measurement of this very family
// reported "60 of 60 identical" because a sed used | as both delimiter and
// alternation, failed, and compared an empty string to an empty string —
// an error that failed toward the cleaner answer, which is the direction
// nobody questions (recon-004). So normalise() refuses to return a value that
// lost everything: if the normalised form is empty while the input was not,
// that is a broken normaliser, not an equal page.

import { createHash } from 'node:crypto';

/**
 * Remove the two differences a depth-duplicate is ALLOWED to have, and
 * nothing else. Whitespace is collapsed because the two copies are usually
 * pretty-printed by different passes; text content is untouched.
 */
export function normalize(html, fromPath = '') {
  const src = String(html ?? '');
  // Strip the depth PREFIX; do not try to resolve the reference.
  //
  // Resolving is the theoretically right thing and it was measured to be much
  // worse: over all 116 colliding pairs in the queue, prefix-stripping proves
  // 88 equal and full resolution only 29. The reason is that these exporters
  // do not agree on how depth is expressed — some write ../, some ship a
  // <base href="../">, some spell a self-link long from the shallow copy and
  // short from the deep one — so resolution has to be right about all three
  // conventions at once, while stripping only has to ignore the one that
  // actually varies. The measurement decided this, not the theory.
  //
  // The cost is a known blind spot, kept deliberately: a pair whose ONLY
  // difference is a self-referential link written at two depths reads as
  // different and stays a refusal (darkrise-nextjs's 404). A refusal that
  // names both files is a cheap wrong answer; a merge of two genuinely
  // different pages is an expensive one.
  const out = src
    .replace(/((?:href|src|srcset|action|poster)\s*=\s*")(?:\.\.\/)+/gi, '$1')
    .replace(/(url\(\s*['"]?)(?:\.\.\/)+/gi, '$1')
    .replace(/<base\b[^>]*>/gi, '')
    .replace(/\s+/g, ' ')
    .trim();
  // CANARY — see the header. A normaliser that eats the document is a bug
  // that reads as a match.
  if (src.trim().length > 0 && out.length === 0) {
    throw new Error('page-equivalence: normalisation produced an empty document — the normaliser is broken, not the pages equal');
  }
  return out;
}

export function fingerprint(html, fromPath = '') {
  return createHash('sha1').update(normalize(html, fromPath), 'utf8').digest('hex').slice(0, 12);
}

/**
 * @returns {{equal: boolean, flatHash: string, dirHash: string}}
 */
export function proveEquivalent(flatHtml, dirHtml, flatPath = '', dirPath = '') {
  const flatHash = fingerprint(flatHtml, flatPath);
  const dirHash = fingerprint(dirHtml, dirPath);
  return { equal: flatHash === dirHash, flatHash, dirHash };
}

/**
 * Which spelling of the address does the site itself use?
 *
 * The answer decides which file survives a merge. It is per-site and it does
 * NOT generalise: bigspring-nextjs points 156 of its own links at about.html
 * and none at about/, while mobit does the exact reverse.
 *
 * A tie — including 0:0, an orphan page nothing links to — resolves to the
 * directory form, because that is the shape the address ends up in once the
 * site is WordPress. The tie-break has to be deterministic; "whichever the
 * scan happened to see first" is how a conversion stops being reproducible.
 */
export function addressOf(file) {
  return /\/index\.html?$/i.test(file) ? file.replace(/index\.html?$/i, '') : file;
}

/**
 * Count how often the site links each spelling, and pick the survivor.
 *
 * The pair is not always `about.html` + `about/index.html`. A static exporter
 * flattens depth into the filename too, so the same page arrives as
 * `blog-page-2.html` + `blog/page/2/index.html`, or as two plain files at
 * different depths (`blog-post-1.html` + `blog/post-1.html`). Counting has to
 * work off the real addresses, not a spelling guessed from the key.
 *
 * The tie-break — which includes 0:0, an orphan nothing links to — prefers the
 * DEEPER address: it mirrors the source's own structure and is the shape the
 * URL takes once the site is WordPress. It must be deterministic; "whichever
 * the scan saw first" is how a conversion stops being reproducible.
 */
export function chooseSurvivor(fileA, fileB, allHtml) {
  const [addrA, addrB] = [addressOf(fileA), addressOf(fileB)];
  const count = (addr) => {
    const re = new RegExp(`(?:href|src)\\s*=\\s*"[^"]*${escapeRe(addr)}"`, 'gi');
    let n = 0;
    for (const html of allHtml) n += (String(html).match(re) || []).length;
    return n;
  };
  const a = count(addrA), b = count(addrB);
  const depth = (f) => f.split('/').length;
  let keptFile;
  if (a !== b) keptFile = a > b ? fileA : fileB;
  else keptFile = depth(fileA) >= depth(fileB) ? fileA : fileB;
  return {
    kept: keptFile,
    dropped: keptFile === fileA ? fileB : fileA,
    links: { [addrA]: a, [addrB]: b },
    tie: a === b,
  };
}

function escapeRe(s) {
  return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
