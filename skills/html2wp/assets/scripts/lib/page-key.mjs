// The ONE derivation of a page's key, shared by stage 0 and stage 4.
//
// It used to be two copies of the same three lines — one in analyze-input, one
// in dist-to-bundle — each keying a page by its filename BASENAME. That is
// fine for a flat site and fatal for a nested one: `blog/index.html`,
// `team/index.html` and `work/index.html` all became "front-page", and
// `blog/posts/1.html` collided with `work/1.html`. Measured on a real input
// (aubergine, 5 root pages / 112 files): 14 collisions, analyze-input exit 2,
// stage 0 refused outright. Both skill versions refused identically, which is
// what made it a limit rather than a regression.
//
// The key is now derived from the path RELATIVE TO THE INPUT ROOT:
//
//   index.html            → front-page
//   about.html            → about            (unchanged: flat stays flat)
//   blog/index.html       → blog             (a directory index names its directory)
//   blog/tags/index.html  → blog-tags
//   blog/posts/1.html     → blog-posts-1
//   work/1.html           → work-1
//
// Backward compatibility is not an assumption here: all eight sites converted
// so far are flat, verified before the change, so every existing key is
// byte-identical under the new rule.

/**
 * @param {string} relPath path relative to the input/dist root, e.g. "blog/posts/1.html"
 * @returns {string} the page key
 */
export function pageKey(relPath) {
  const clean = String(relPath || '')
    .replace(/\\/g, '/')
    .replace(/^\.?\//, '')
    .replace(/[#?].*$/, '')
    .replace(/\.html?$/i, '');
  const segs = clean.split('/').filter(Boolean);
  // A directory's index names the DIRECTORY, not the front page — otherwise
  // every nested index collapses onto one key, which is exactly the collision
  // this exists to end.
  if (segs.length && segs[segs.length - 1].toLowerCase() === 'index') segs.pop();
  if (!segs.length) return 'front-page';
  // Exactly the alphabet the service stores (transform.ts PAGE_KEY:
  // /^[a-z0-9][a-z0-9-]{0,95}$/). An underscore used to survive here, so
  // `index_v2.html` or `about_us.html` derived a key both validators refuse —
  // stage 0 exited 2 on an ordinary flat site. A manifest override could not
  // repair it: the bundle keys a plain page by THIS derivation, never by the
  // manifest's key. A leading separator (`_draft.html`) is dropped for the same
  // grammar; whatever else two files now share is the collision check's job.
  const key = segs.join('-').toLowerCase().replace(/[^a-z0-9-]/g, '-').replace(/^-+/, '');
  return key || 'page';
}

/**
 * Resolve a link target written on `fromPath` into a page key.
 * A nested page links its siblings relative to ITS OWN directory
 * (`../about.html`, `posts/1.html`), so the href has to be resolved before it
 * can be keyed — keying the raw href would map `../about.html` and
 * `about.html` to two different pages, or to the wrong one.
 */
export function pageKeyOfLink(href, fromPath = '') {
  const raw = String(href || '').replace(/[#?].*$/, '');
  if (!raw) return null;
  if (raw.startsWith('/')) return pageKey(raw.slice(1));
  const dir = String(fromPath || '').replace(/\\/g, '/').split('/').slice(0, -1);
  const segs = [...dir];
  for (const part of raw.split('/')) {
    if (part === '.' || part === '') continue;
    if (part === '..') { segs.pop(); continue; }
    segs.push(part);
  }
  return pageKey(segs.join('/'));
}
