/**
 * What must not travel with the site.
 *
 * Stage 1 copies every non-HTML file from the input directory into
 * `astro-project/public/`, and stage 3 tars that whole tree and uploads it.
 * The filter used to be four directory names (`node_modules`, `.git`,
 * `.astro`, `_original`) — so `.env`, `id_rsa`, `credentials.json`, `.npmrc`
 * and `~/.aws/` all rode along to the service, twice over, because Astro's
 * `public/` is a pass-through into `dist/` as well.
 *
 * The user did not choose to send those. They pointed the converter at a
 * folder, and a folder that a person has been working in has things in it.
 *
 * Two filters, both on by default, because either one alone loses:
 *
 *   an ALLOWLIST of web asset extensions decides what may travel at all. This
 *             is the one that holds, because it does not have to predict the
 *             name of the secret. `H2WP_ASSET_ALLOWLIST=0` turns it off for a
 *             site that genuinely needs an unusual type.
 *   a DENYLIST of secret shapes and content markers then refuses the files
 *             that ARE web asset types but should still not go — a private
 *             key pasted into a .txt, an access key in a config.json.
 *
 * The allowlist used to be the opt-in half, which left the denylist carrying
 * it alone: `.envrc` walked past the `.env` pattern, a backup `.zip` was
 * never opened, and anything above the content-scan limit was never read.
 * A denylist has to name the secret, and the interesting file is always the
 * one nobody thought of.
 *
 * A skipped file is a WARNING and the conversion continues — reported into
 * astro-report.json and counted on the console, so a site that needed it says
 * so out loud. The build missing an asset is a better failure than a silent
 * upload of somebody's private key.
 */

const SECRET_NAMES = [
  /^\.env(rc|\..*)?$/i, // .env, .envrc, .env.local, .env.production
  /^\.npmrc$/i,
  /^\.yarnrc(\.yml)?$/i,
  /^\.pypirc$/i,
  /^\.netrc$/i,
  /^\.htpasswd$/i,
  /^id_rsa$/i,
  /^id_dsa$/i,
  /^id_ecdsa$/i,
  /^id_ed25519$/i,
  /^credentials(\.json|\.yml|\.yaml)?$/i,
  /^service-account.*\.json$/i,
  /^.*\.pem$/i,
  /^.*\.key$/i,
  /^.*\.p12$/i,
  /^.*\.pfx$/i,
  /^.*\.keystore$/i,
  /^.*\.jks$/i,
  /^secrets?(\.json|\.yml|\.yaml|\.toml)$/i,
  /^terraform\.tfvars$/i,
  /^.*\.sqlite3?$/i,
  /^.*\.sql$/i,
  // An archive is opaque to the content scan, so whatever is inside it
  // travels unexamined. Never something a built site needs.
  /^.*\.(zip|tar|tgz|gz|bz2|xz|7z|rar)$/i,
  /^.*\.(bak|backup|dump)$/i,
];

/** Directory names that are never part of a website. */
const SECRET_DIRS = new Set([
  '.ssh',
  '.aws',
  '.gcloud',
  '.azure',
  '.docker',
  '.gnupg',
  '.kube',
  '.terraform',
  'node_modules',
  '.git',
  '.hg',
  '.svn',
  '.astro',
  '_original',
  '.vscode',
  '.idea',
]);

/** Extensions a static site actually serves. Only consulted in strict mode. */
export const WEB_ASSET_EXTENSIONS = new Set([
  '.css', '.js', '.mjs', '.cjs', '.map', '.json', '.jsonld', '.xml', '.txt', '.webmanifest',
  '.svg', '.svgz', '.png', '.apng', '.jpg', '.jpeg', '.jfif', '.webp', '.avif', '.gif',
  '.ico', '.bmp', '.cur',
  '.woff', '.woff2', '.ttf', '.otf', '.eot',
  '.mp4', '.webm', '.ogv', '.mov', '.mp3', '.ogg', '.oga', '.wav', '.m4a', '.aac',
  '.opus', '.flac',
  // Streaming video ships as a playlist plus numbered segments; dropping
  // either leaves a player that loads and plays nothing.
  '.m3u8', '.mpd',
  '.pdf', '.wasm', '.vtt', '.srt',
  // 3D and AR, which a modern marketing site does use: geometry and textures
  // ride beside the .glb rather than inside it.
  '.glb', '.gltf', '.bin', '.hdr', '.exr', '.ktx2', '.basis', '.drc', '.usdz',
  '.csv',
]);

/**
 * Content markers, kept deliberately few. A scanner with false positives gets
 * switched off, and then it protects nothing — so this only carries patterns
 * that do not occur in a website by accident.
 */
// `SEP` is the gap between a key's NAME and its value in the three dialects
// these files come in: `KEY=v` (.env, ini), `KEY: v` (YAML) and
// `"KEY": "v"` (JSON). The closing quote before the colon is why a first
// version of this scanner read straight past
// {"AWS_SECRET_ACCESS_KEY": "..."} — the test caught it.
const SEP = String.raw`["']?\s*[:=]\s*["']?\S`;

const SECRET_CONTENT = [
  [/-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----/, 'it contains a private key block'],
  [new RegExp(String.raw`\bAWS_SECRET_ACCESS_KEY${SEP}`, 'i'), 'it contains an AWS secret key'],
  [new RegExp(String.raw`\b(?:DATABASE_PASSWORD|DB_PASSWORD|MYSQL_PASSWORD|POSTGRES_PASSWORD)${SEP}`, 'i'), 'it contains a database password'],
  [new RegExp(String.raw`\bPRIVATE_KEY${SEP}`, 'i'), 'it contains a private key'],
  [new RegExp(String.raw`\b(?:SECRET_ACCESS_KEY|CLIENT_SECRET)${SEP}`, 'i'), 'it contains a client secret'],
  // Distinctive token shapes. The env-var names above catch a key by the
  // slot it sits in; these catch it by the key itself, for the file that
  // hands it over with no telling name — `const stripe = require('stripe')
  // ('sk_live_…')` in an ordinary `config.js`. Every prefix here belongs to a
  // SERVER-side secret that a website never emits by accident.
  //
  // Deliberately NOT here — the publishable halves that legitimately ship
  // inside a static site, so matching them would drop an asset the page
  // needs: Stripe `pk_live_`, Google/Maps browser keys `AIza…`, Firebase web
  // config, and bare `eyJ…` JWTs (a Supabase anon key is one). The rule is
  // that a marker only earns its place if it cannot occur in a real site.
  [/\b[sr]k_(?:live|test)_[0-9a-zA-Z]{16,}/, 'it contains a Stripe secret key'],
  [/\bgh[pousr]_[0-9A-Za-z]{36}\b/, 'it contains a GitHub token'],
  [/\bgithub_pat_[0-9A-Za-z_]{22,}/, 'it contains a GitHub token'],
  [/\bAKIA[0-9A-Z]{16}\b/, 'it contains an AWS access key id'],
  [/\bASIA[0-9A-Z]{16}\b/, 'it contains an AWS temporary access key id'],
  [/\bxox[baprs]-[0-9A-Za-z]{10,}/, 'it contains a Slack token'],
];

/** Extensions never worth reading for content markers. */
const OPAQUE = new Set([
  '.png', '.jpg', '.jpeg', '.webp', '.avif', '.gif', '.ico', '.bmp',
  '.woff', '.woff2', '.ttf', '.otf', '.eot',
  '.mp4', '.webm', '.ogv', '.mp3', '.ogg', '.wav', '.m4a',
  '.pdf', '.wasm', '.zip', '.gz', '.tgz', '.glb',
]);

const extensionOf = (rel) => {
  const base = rel.slice(rel.lastIndexOf('/') + 1);
  const dot = base.lastIndexOf('.');
  return dot <= 0 ? '' : base.slice(dot).toLowerCase();
};

/**
 * Largest file worth scanning for markers.
 *
 * A key is never megabytes long, but the FILE carrying it routinely is: a
 * bundled `app.js` runs past a megabyte and is exactly where an inlined
 * server key ends up. At 512 KB those went unread — the scan silently stopped
 * at the size where real bundles begin. Four megabytes covers them and still
 * refuses to read a video into memory.
 */
export const SCAN_LIMIT_BYTES = 4 * 1024 * 1024;

/**
 * Decide on one file, by path alone.
 *
 * @param {string} rel      path relative to the input root, forward slashes
 * @param {{strict?: boolean}} [opts]
 * @returns {{copy: boolean, reason?: string}}
 */
export function assetVerdict(rel, opts = {}) {
  const segments = rel.split('/');
  const name = segments[segments.length - 1];

  for (const segment of segments.slice(0, -1)) {
    if (SECRET_DIRS.has(segment)) return { copy: false, reason: `it is inside ${segment}/` };
  }
  if (SECRET_DIRS.has(name)) return { copy: false, reason: `it is ${name}` };
  if (name === '.DS_Store' || name.startsWith('._')) return { copy: false, reason: 'it is macOS metadata' };

  for (const pattern of SECRET_NAMES) {
    if (pattern.test(name)) return { copy: false, reason: `${name} is the shape of a credential file` };
  }

  if (opts.strict) {
    const ext = extensionOf(rel);
    if (!WEB_ASSET_EXTENSIONS.has(ext)) {
      return { copy: false, reason: `${ext || 'no extension'} is not a web asset type (strict mode)` };
    }
  }

  return { copy: true };
}

/**
 * Second look, at the bytes. Catches the file whose NAME says nothing —
 * `config.json` holding an access key, `notes.txt` with a pasted key block.
 *
 * @param {string} rel
 * @param {() => Buffer} readBytes  called only when it is worth reading
 * @param {number} size
 * @returns {string|null}  reason to skip, or null to keep
 */
export function secretContentReason(rel, readBytes, size) {
  if (size > SCAN_LIMIT_BYTES || size === 0) return null;
  if (OPAQUE.has(extensionOf(rel))) return null;
  let text;
  try {
    text = readBytes().toString('utf8');
  } catch {
    return null;
  }
  // A NUL in the first block means binary; the markers are all ASCII.
  if (text.indexOf('\0') !== -1) return null;
  for (const [pattern, reason] of SECRET_CONTENT) {
    if (pattern.test(text)) return reason;
  }
  return null;
}

/** Is the strict allowlist on? Read once by the caller, not per file. */
/**
 * The allowlist is the DEFAULT, and turning it off is the deliberate act.
 *
 * It was the other way around, and the denylist left on its own is the half
 * that cannot win: it has to name the secret, and the interesting file is
 * always the one nobody thought of. Measured against this filter, `.envrc`
 * walked past the `.env` pattern, a backup `.zip` was never opened, and a
 * credential in a file above the content-scan limit was never read. None of
 * those are bugs in the patterns — they are the shape of a denylist.
 *
 * A website needs a closed set of file types to render, so that set is what
 * travels and everything else stays on the machine it came from. What gets
 * left out is reported into astro-report.json and counted on the console, so
 * a site that genuinely needs an unusual type says so out loud instead of
 * silently converting without it — and `H2WP_ASSET_ALLOWLIST=0` is then the
 * way to let it through.
 */
export const strictAssetMode = () => process.env['H2WP_ASSET_ALLOWLIST'] !== '0';
