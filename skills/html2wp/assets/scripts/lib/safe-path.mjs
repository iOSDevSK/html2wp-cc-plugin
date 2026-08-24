/**
 * One answer to "is this path still inside the directory I meant?".
 *
 * Every path the generators join onto DIST comes from somewhere the caller
 * controls: `pages[].file` is written by the customer's assistant into the
 * manifest, and media refs are scraped out of the customer's own HTML. Both
 * arrive over the wire. `join()` normalizes `..` away silently, so
 * `join(DIST, '../../app/core/visual-edit.zip')` is not an error — it is a
 * working read of the paid plugin, and whatever it reads lands in the theme
 * the caller then downloads. That is the entitlement model defeated by a
 * text field.
 *
 * The other half is symlinks. `statSync` follows them, so a link planted in
 * the uploaded workspace makes an in-bounds path resolve out of bounds. A
 * containment check on the joined string cannot see that; only realpath can.
 *
 * Returns the absolute path when it is safe, or null. Null is deliberately
 * not an exception: an unusable page or a bad image reference is a warning
 * and a skipped item, never a dead conversion (SKILL.md's rule — unsafe
 * OPTIONAL input is removed and the run continues).
 */

import { lstatSync, realpathSync } from 'node:fs';
import { basename, dirname, isAbsolute, resolve, sep } from 'node:path';

/**
 * What a directory entry is, without following it.
 *
 * `statSync` reports a symlink as whatever it POINTS AT, so every walk in this
 * pipeline used to treat `assets -> ~/.ssh` as an ordinary directory, recurse
 * into it, and hand the caller its contents as if they were part of the site.
 * From there they were copied into `public/`, built into `dist/`, and uploaded.
 * There was not one `lstat` in the whole repo.
 *
 * Returns 'file' | 'dir' | null. null means SKIP: a symlink, a device node, a
 * socket, a FIFO — nothing a website is made of, and the walkers each warn in
 * their own voice rather than having an opinion imposed here.
 */
export function entryKind(fullPath) {
  let st;
  try {
    st = lstatSync(fullPath);
  } catch {
    return null;
  }
  if (st.isSymbolicLink()) return null;
  if (st.isDirectory()) return 'dir';
  if (st.isFile()) return 'file';
  return null;
}

/** Is `p` the directory `root` itself, or something underneath it? */
function contains(root, p) {
  if (p === root) return true;
  return p.startsWith(root.endsWith(sep) ? root : root + sep);
}

/**
 * realpath the deepest part of `p` that actually exists, then re-attach the
 * rest. Plain realpathSync throws ENOENT on a path that has not been written
 * yet, which is the normal case for a write target — but its PARENT may still
 * be a symlink pointing somewhere else, and that is exactly what we need to
 * catch. Anything other than ENOENT (a permission wall, a loop) is reported
 * as the literal path and left to the containment check.
 */
function realpathDeepest(p) {
  const tail = [];
  let head = p;
  for (;;) {
    try {
      const real = realpathSync(head);
      return tail.length ? resolve(real, ...tail.slice().reverse()) : real;
    } catch (err) {
      if (err.code !== 'ENOENT') return p;
      const parent = dirname(head);
      if (parent === head) return p; // walked to the filesystem root, nothing existed
      tail.push(basename(head));
      head = parent;
    }
  }
}

/**
 * @param {string} root      the directory the result must stay inside
 * @param {string} relative  caller-supplied, and therefore untrusted
 * @returns {string|null}    absolute path, or null when it is not safe
 */
export function safePathUnderRoot(root, relative) {
  if (typeof relative !== 'string' || relative === '') return null;
  // A NUL truncates the path down in the syscall layer, so what the checks
  // above see and what open(2) opens stop being the same string.
  if (relative.includes('\0')) return null;
  // Absolute in any dialect. isAbsolute() is POSIX-only on this platform, so
  // a Windows drive letter or a UNC path would otherwise read as relative and
  // then join into something surprising.
  if (isAbsolute(relative) || /^[a-zA-Z]:[\\/]/.test(relative) || relative.startsWith('\\\\')) return null;

  // Any surviving `..` is refused outright rather than resolved, because
  // lexical and physical resolution DISAGREE the moment a symlink is in the
  // path and the disagreement is exploitable. `resolve()` collapses
  // `self/../outside` to `outside` by string arithmetic; the kernel follows
  // `self` to its target FIRST and only then applies `..`, so the two land in
  // different directories and the containment check below would be validating
  // a path nobody is going to open. Caught by test-safe-path.sh, which is the
  // only reason this comment exists.
  //
  // Nothing legitimate is lost: every caller composes with join(), which has
  // already collapsed the `..` a nested page's relative refs need
  // (join('blog', '../assets/y.jpg') === 'assets/y.jpg'). A `..` that is still
  // here after that is one that escaped, which is the case we refuse anyway.
  if (relative.split(/[\\/]/).includes('..')) return null;

  const base = realpathDeepest(resolve(root));
  const target = resolve(base, relative);
  if (!contains(base, target)) return null;
  // And again after following links, because the string can be innocent while
  // the inode is not.
  if (!contains(base, realpathDeepest(target))) return null;

  return target;
}
