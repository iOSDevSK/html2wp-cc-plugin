/**
 * Where a conversion manifest's paths really point.
 *
 * A workspace is the directory that holds its conversion-manifest.json. The
 * manifest also RECORDS that directory ("workspace") and its input
 * ("input.dir") as absolute paths — so a copied workspace (a scratch run, a
 * second agent, a backup) used to read and write the ORIGINAL through them: a
 * stage run on the copy rewrote the original's theme. The manifest's own
 * location wins; an input.dir inside the recorded workspace moves with it; a
 * relative one is relative to the workspace; an input elsewhere stays.
 *
 * Mirrored by lib/manifest_paths.py — the same rule for the Python stages.
 */
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';

/**
 * The workspace: the directory of its conversion-manifest.json. A manifest
 * under any other name (a fixture, a hand-made one) keeps its recorded
 * workspace.
 */
export function workspaceOf(mf, manifestPath) {
  const here = resolve(manifestPath);
  if (basename(here) === 'conversion-manifest.json' || !(mf && mf.workspace)) return dirname(here);
  return resolve(mf.workspace);
}

/** input.dir, resolved against the workspace the manifest is actually in. */
export function inputDirOf(mf, manifestPath) {
  const ws = workspaceOf(mf, manifestPath);
  const raw = String(((mf && mf.input) || {}).dir || '').trim();
  if (!raw) return join(ws, 'static-src');
  if (!isAbsolute(raw)) return resolve(ws, raw);
  const declared = mf && mf.workspace ? resolve(mf.workspace) : null;
  const p = resolve(raw);
  if (declared && declared !== ws && (p === declared || p.startsWith(declared + sep))) {
    return join(ws, relative(declared, p));
  }
  return p;
}
