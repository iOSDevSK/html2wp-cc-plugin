// Exercises the SHIPPED helper — imported, not re-declared, because the whole
// point of lib/safe-path.mjs is that there is exactly one copy of this answer.
//
// The symlink cases build real links in a temp tree: a containment check that
// only looks at the string passes them, and that is precisely the bug.
import { mkdtempSync, mkdirSync, writeFileSync, symlinkSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { safePathUnderRoot } from './lib/safe-path.mjs';

const ROOT = mkdtempSync(join(tmpdir(), 'h2wp-safe-path-'));
const DIST = join(ROOT, 'dist');
const OUTSIDE = join(ROOT, 'outside');
mkdirSync(join(DIST, 'blog'), { recursive: true });
mkdirSync(OUTSIDE, { recursive: true });
writeFileSync(join(DIST, 'index.html'), 'ok');
writeFileSync(join(DIST, 'blog', 'post.html'), 'ok');
writeFileSync(join(OUTSIDE, 'secret.zip'), 'the paid plugin');
// assets -> ../outside : the shape an uploaded workspace can carry.
symlinkSync(OUTSIDE, join(DIST, 'assets'));
// self -> . : the loop that makes an unguarded walk recurse until ELOOP.
symlinkSync(DIST, join(DIST, 'self'));

const ALLOWED = 'allowed';
const REFUSED = 'refused';
const verdict = (rel) => (safePathUnderRoot(DIST, rel) === null ? REFUSED : ALLOWED);

const cases = [
  // --- must pass: the ordinary traffic this helper sits in front of ---
  ['index.html', ALLOWED],
  ['blog/post.html', ALLOWED],
  ['./blog/post.html', ALLOWED],
  ['not-written-yet.html', ALLOWED],        // a write target need not exist
  ['deep/nested/new.html', ALLOWED],        // nor its parents
  ['..hidden.html', ALLOWED],               // leading dots are not a traversal
  ['file with spaces.png', ALLOWED],
  ['ünïcode.webp', ALLOWED],

  // --- must fail: traversal, in the forms join() collapses silently ---
  ['../secret', REFUSED],
  ['../../etc/passwd', REFUSED],
  ['../outside/secret.zip', REFUSED],
  ['blog/../../outside/secret.zip', REFUSED],
  ['a/b/c/../../../../outside/secret.zip', REFUSED],
  ['./../../app/core/visual-edit.zip', REFUSED],
  ['blog/../index.html', REFUSED],          // lands inside, still refused: see the
                                            // lexical-vs-physical note in safe-path.mjs
  ['..\\..\\outside\\secret.zip', REFUSED], // backslash separators too

  // --- must fail: absolute, in every dialect ---
  ['/etc/passwd', REFUSED],
  ['/app/core/visual-edit.zip', REFUSED],
  ['C:\\Windows\\system32', REFUSED],
  ['\\\\server\\share\\file', REFUSED],

  // --- must fail: symlinks, where the string is innocent and the inode is not ---
  ['assets/secret.zip', REFUSED],           // through the planted link
  ['assets', REFUSED],                      // the link itself
  ['self/../outside/secret.zip', REFUSED],  // loop used as a ladder

  // --- must fail: not a usable path at all ---
  ['', REFUSED],
  ['bad\0.html', REFUSED],
  [null, REFUSED],
  [undefined, REFUSED],
  [42, REFUSED],
  [{}, REFUSED],
];

let failed = 0;
for (const [input, want] of cases) {
  let got;
  try {
    got = verdict(input);
  } catch (err) {
    got = `threw ${err.code || err.name}`; // throwing is a failure too: callers rely on null
  }
  const shown = typeof input === 'string' ? JSON.stringify(input) : String(input);
  if (got !== want) {
    console.error(`  FAIL ${shown}: wanted ${want}, got ${got}`);
    failed += 1;
  }
}

// The `self` loop must also not hang the helper — the assertion above only
// proves the verdict, not that we got one in finite time.
const started = Date.now();
safePathUnderRoot(DIST, 'self/self/self/self/self/self/self/self/index.html');
if (Date.now() - started > 2000) {
  console.error('  FAIL symlink loop took over 2s — bounded resolution is the requirement');
  failed += 1;
}

rmSync(ROOT, { recursive: true, force: true });

if (failed) {
  console.error(`${failed} of ${cases.length + 1} cases failed`);
  process.exit(1);
}
console.log(`  ${cases.length + 1} cases pass`);
