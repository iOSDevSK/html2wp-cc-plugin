// Exercises the SHIPPED derivation — imported, not re-declared: stage 0 and
// the service's stage 4 both key pages through lib/page-key.mjs, and every key
// it derives must be one the service's own validator (transform.ts PAGE_KEY)
// stores. A key outside that grammar used to refuse the whole site at stage 0.
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { pageKey, pageKeyOfLink } from './lib/page-key.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const PAGE_KEY = /^[a-z0-9][a-z0-9-]{0,95}$/;

const keys = [
  // flat and nested shapes whose keys earlier conversions already rely on
  ['index.html', 'front-page'],
  ['INDEX.HTM', 'front-page'],
  ['about.html', 'about'],
  ['blog/index.html', 'blog'],
  ['blog/tags/index.html', 'blog-tags'],
  ['blog/posts/1.html', 'blog-posts-1'],
  ['./work/1.html', 'work-1'],
  ['docs\\guide.html', 'docs-guide'],
  // characters outside the service's alphabet
  ['index_v2.html', 'index-v2'],
  ['about_us.html', 'about-us'],
  ['blog/my_first_post.html', 'blog-my-first-post'],
  ['Foo Bar.html', 'foo-bar'],
  ['café.html', 'caf-'],
  ['2024_recap.html', '2024-recap'],
  ['_draft.html', 'draft'],
  ['__/x.html', 'x'],
  ['_.html', 'page'],
];

let fail = 0;
for (const [input, want] of keys) {
  const got = pageKey(input);
  const ok = got === want && PAGE_KEY.test(got);
  if (!ok) fail++;
  console.log(`  ${ok ? 'ok  ' : 'FAIL'} pageKey(${JSON.stringify(input)}) = ${JSON.stringify(got)}` +
    (got === want ? '' : ` (want ${JSON.stringify(want)})`) + (PAGE_KEY.test(got) ? '' : ' — outside PAGE_KEY'));
}

// A link resolves to the key of the page it names, whatever form it takes.
const links = [
  ['index_v9.html', 'about.html', 'index-v9'],
  ['../about_us.html#team', 'blog/post.html', 'about-us'],
  ['/services_old.html?x=1', 'a/b.html', 'services-old'],
];
for (const [href, from, want] of links) {
  const got = pageKeyOfLink(href, from);
  if (got !== want) fail++;
  console.log(`  ${got === want ? 'ok  ' : 'FAIL'} pageKeyOfLink(${JSON.stringify(href)}, ${JSON.stringify(from)}) = ${JSON.stringify(got)}`);
}

// Length is the one limit the derivation cannot fix; it stays a stage-0 error.
const long = pageKey(`${'a'.repeat(100)}.html`);
if (PAGE_KEY.test(long)) { fail++; console.log('  FAIL a 100-character key passed PAGE_KEY'); }
else console.log('  ok   a 100-character key is still refused by PAGE_KEY');

// The service's copy must be this copy, byte for byte, or the two stages key
// the same site differently.
const server = join(HERE, '..', '..', '..', '..', '..', 'server', 'core', 'scripts', 'lib', 'page-key.mjs');
try {
  const same = readFileSync(server, 'utf8') === readFileSync(join(HERE, 'lib', 'page-key.mjs'), 'utf8');
  if (!same) fail++;
  console.log(`  ${same ? 'ok  ' : 'FAIL'} server/core/scripts/lib/page-key.mjs is identical`);
} catch {
  console.log('  skip server copy not present (skill installed on its own)');
}

// And the validator in transform.ts is still the grammar this test holds keys to.
const transform = join(HERE, '..', '..', '..', '..', '..', 'server', 'src', 'transform.ts');
try {
  const src = readFileSync(transform, 'utf8');
  const same = src.includes(`const PAGE_KEY = ${PAGE_KEY.toString()};`);
  if (!same) fail++;
  console.log(`  ${same ? 'ok  ' : 'FAIL'} transform.ts PAGE_KEY is ${PAGE_KEY}`);
} catch {
  console.log('  skip server/src/transform.ts not present');
}

if (fail) { console.error(`${fail} page-key case(s) failed`); process.exit(1); }
console.log('page-key: all cases passed');
