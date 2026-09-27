#!/usr/bin/env python3
"""Repair an Astro delivery in an isolated candidate; keep its previous ZIP on failure.

astro-delivery.py WORKSPACE repair
Only an explicit owner repair request may run this command. Questions do not.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'lib'))
import astro_delivery as astro
import theme_state
from full_delivery import read, write
from repair_budget import open_attempt, close_attempt


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(candidate, log, command, allowed_failure=False, cwd=None):
    env = dict(os.environ, H2WP_WORKSPACE=str(candidate), H2WP_OUTPUT_DIR=str(candidate / 'out'), H2WP_MODE='astro')
    with log.open('a') as output:
        output.write('\nCOMMAND ' + ' '.join(map(str, command)) + '\n'); output.flush()
        result = subprocess.run(list(map(str, command)), cwd=cwd or candidate, env=env,
                                stdout=output, stderr=subprocess.STDOUT, timeout=3600)
    if result.returncode and not allowed_failure:
        raise RuntimeError(f'{Path(str(command[1] if command[0] in (sys.executable, "node") else command[0])).name} failed; see {log.name}')
    return result.returncode


def mirror_wrappers(ws, archive):
    """Recognize byte-identical raw wrappers, not arbitrary owner edits.

    The old '0 pages' remedy added these files without changing content.
    Keep the owner's route wrapper and point its raw document at the fresh
    capture. Any content edit or different wrapper remains a merge conflict.
    """
    import re
    project = ws / 'astro-project'
    base = theme_state.astro_zip_tree(archive, ('src', 'public'))
    found = []
    for route in (project / 'src/pages').glob('*.astro'):
        stem = route.stem
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', stem):
            continue
        key = f'src/pages/{stem}.astro'
        raw = project / f'src/html/{stem}.html'
        public = project / f'public/{stem}.html'
        expected = f"---\nimport html from '../html/{stem}.html?raw';\n---\n<Fragment set:html={{html}} />\n"
        if key not in base and f'src/html/{stem}.html' not in base and raw.is_file() and public.is_file() \
                and route.read_text() == expected and sha(raw) == sha(public) == base.get(f'public/{stem}.html'):
            found.append(stem)
    return found


def owner_changes(ws, archive):
    current = theme_state.astro_tree(ws / 'astro-project')
    base = theme_state.astro_zip_tree(archive, ('src', 'public'))
    allowed = {name for stem in mirror_wrappers(ws, archive)
               for name in (f'src/pages/{stem}.astro', f'src/html/{stem}.html')}
    return [name for name in theme_state.differ(base, current) if name not in allowed]


def merge_mirror_wrappers(ws, archive, candidate):
    for stem in mirror_wrappers(ws, archive):
        project = candidate / 'astro-project'
        route = project / f'src/pages/{stem}.astro'
        source = project / f'public/{stem}.html'
        if route.exists() or not source.is_file():
            raise RuntimeError(f'Owner wrapper {stem} conflicts with the regenerated route')
        route.parent.mkdir(parents=True, exist_ok=True)
        (project / 'src/html').mkdir(parents=True, exist_ok=True)
        shutil.copy2(ws / f'astro-project/src/pages/{stem}.astro', route)
        shutil.copy2(source, project / f'src/html/{stem}.html')


def publish_retained(ws, out, base, note):
    result = dict(base)
    result['repairs'] = (read(ws / 'progress.json') or {}).get('repairs', [])
    result['recovery'] = {'policy': astro.POLICY, 'available': True, 'action': 'repair-delivery', 'stages': ['-1']}
    result['verdict'] = 'Astro: previous ZIP retained; repair incomplete'
    result['repairRequestId'] = os.environ.get('H2WP_TURN')
    result['couldNotFix'] = [{'stage': '-1', 'what': note}]
    result['writtenAt'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    write(ws / 'result.json', result); write(out / 'result.json', result)
    return result


WORKSPACE_FILES = ('astro-project', 'static-src', 'input-untouched', '.static-src-source-build',
    'conversion-manifest.json', 'route-inventory.json', 'detect.json', 'analysis.json',
    'flash-candidates.json', 'astro-report.json', 'astro-coverage.json', 'prerender-report.json',
    'source-assets-report.json', 'optimize-images-report.json', 'optimize-markup-report.json',
    'verify-static', 'parity-report.json', 'parity-report', 'CONVERSION-REPORT.md',
    '.astro-applied.json', 'changes.json', 'progress.json')


def publish_candidate(ws, out, history, candidate, result, export_name):
    """Publish only known outputs; roll back every rename on any failure.

    out/result.json is last: the app sees either the old delivery or the new
    complete one. Existing exported ZIPs are never replaced.
    """
    if (out / export_name).exists():
        raise RuntimeError('The candidate export name already exists')
    staged = history / 'publication'
    staged.mkdir()
    artifact = candidate / 'out' / result['astro']['file']
    shutil.copy2(artifact, staged / export_name)
    result['astro']['file'] = export_name
    for path in candidate.glob('*.json'):
        if path.name in WORKSPACE_FILES:
            path.write_text(path.read_text().replace(str(candidate), str(ws)))
    actions = [(candidate / name, ws / name) for name in WORKSPACE_FILES if (candidate / name).exists()]
    actions.append((staged / export_name, out / export_name))
    for name in ('CONVERSION-REPORT.md', 'conversion-report.pdf'):
        if (candidate / 'out' / name).is_file():
            shutil.copy2(candidate / 'out' / name, staged / name)
            actions.append((staged / name, out / name))
    for label, dest in (('workspace', ws / 'result.json'), ('output', out / 'result.json')):
        src = staged / (label + '-result.json'); write(src, result); actions.append((src, dest))
    journal = []
    try:
        for i, (src, dest) in enumerate(actions):
            entry = {'src': src, 'dest': dest, 'backup': staged / ('old-' + str(i)), 'old': False, 'new': False}
            journal.append(entry)
            if dest.exists():
                dest.rename(entry['backup']); entry['old'] = True
            src.rename(dest); entry['new'] = True
    except Exception:
        for entry in reversed(journal):
            if entry['new']:
                entry['dest'].rename(entry['src'])
            if entry['old']:
                entry['backup'].rename(entry['dest'])
        raise


def repair(ws):
    out = Path(os.environ.get('H2WP_OUTPUT_DIR') or ws / 'out').resolve()
    turn = os.environ.get('H2WP_TURN')
    if os.environ.get('H2WP_MODE') != 'repair-delivery' or not turn:
        raise RuntimeError('Astro recovery requires an owner repair-delivery turn')
    base = read(ws / 'result.json') or read(out / 'result.json') or {}
    if base.get('target') != 'astro' or base.get('status') != 'delivered':
        raise RuntimeError('No delivered Astro product to repair')
    session = read(ws / 'repair-session.json') or {}
    if session and session.get('target') != 'astro':
        raise RuntimeError('A different target owns the repair session')
    policy = base.get('recovery') or {}
    if policy.get('policy') == astro.POLICY and not policy.get('available'):
        raise RuntimeError('This Astro delivery has no outstanding repair')
    name = (base.get('astro') or {}).get('file', '')
    if not name or Path(name).name != name:
        raise RuntimeError('Invalid previous Astro artifact name')
    archive = next((p / name for p in (out, ws) if astro.valid_zip(p / name)
                    and sha(p / name) == base['astro'].get('sha256')), None)
    if not archive:
        raise RuntimeError('Previous Astro ZIP missing or its hash changed')
    history = ws / 'repair-history' / ('astro-' + str(time.time_ns()))
    history.mkdir(parents=True)
    shutil.copy2(archive, history / name)
    write(history / 'result.json', base)
    if (ws / 'astro-project').is_dir():
        shutil.copytree(ws / 'astro-project', history / 'astro-project',
                        ignore=shutil.ignore_patterns('node_modules', '.git'), symlinks=True)
    write(ws / 'repair-session.json', {'target': 'astro', 'turn': turn, 'base': str(history), 'result': base})
    if open_attempt(ws / 'progress.json', ws / 'result.json', '-1', 'astro-isolated-capture', 'astro-incomplete'):
        raise RuntimeError('Repair allowance exhausted or this remedy already ran in this owner turn')
    candidate = history / 'candidate'
    candidate.mkdir()
    # The report writer resolves host model/timing history beside its workspace.
    context = ws.parent / '.app/model-history.json'
    if context.is_file():
        (history / '.app').mkdir()
        shutil.copy2(context, history / '.app/model-history.json')
    log = history / 'repair.log'
    try:
        changed = owner_changes(ws, archive)
        if changed:
            raise RuntimeError('Owner edits need an explicit merge before upstream regeneration: ' + ', '.join(changed[:8]))
        detection = read(ws / 'detect.json') or {}
        project = Path(detection.get('root') or ws.parent / 'source')
        if not (project / 'package.json').is_file():
            raise RuntimeError('Original web-app root is unavailable; no recapture was attempted')
        inventory = read(ws / 'route-inventory.json') or {}
        prior = read(ws / 'prerender-report.json') or {}
        routes = list(dict.fromkeys(inventory.get('routes', []) + prior.get('routes', [])))
        write(candidate / 'route-inventory.json', {'routes': routes})
        write(candidate / 'detect.json', detection)
        capture = candidate / 'static-src'
        # A concrete change from the failed parallel capture: a new build,
        # isolated workspace and one browser. No overlay of old/new assets.
        run(candidate, log, [sys.executable, HERE / 'prerender-spa.py', '--project', project,
                            '--out', capture, '--no-verify', '--flash', '--jobs', '1'], allowed_failure=True)
        if not list(capture.rglob('*.html')):
            raise RuntimeError('No usable pages captured; old ZIP retained')
        shutil.copytree(capture, candidate / 'input-untouched')
        run(candidate, log, ['node', HERE / 'analyze-input.mjs', capture, '--out=' + str(candidate / 'analysis.json')])
        run(candidate, log, [sys.executable, HERE / 'flash-manifest.py', '--analysis', candidate / 'analysis.json',
                            '--input', capture, '--workspace', candidate])
        mf = read(candidate / 'conversion-manifest.json')
        mf['site'] = (read(ws / 'conversion-manifest.json') or {}).get('site') or base['site']
        write(candidate / 'conversion-manifest.json', mf)
        run(candidate, log, [sys.executable, HERE / 'check-manifest.py', '--manifest=' + str(candidate / 'conversion-manifest.json')])
        run(candidate, log, [sys.executable, HERE / 'optimize-images.py', '--input', capture, '--remote', '--apply', '--out', candidate / 'optimize-images-report.json'])
        run(candidate, log, [sys.executable, HERE / 'optimize-markup.py', '--manifest=' + str(candidate / 'conversion-manifest.json'), '--responsive', '--apply'])
        run(candidate, log, ['node', HERE / 'html-to-astro.mjs', '--manifest=' + str(candidate / 'conversion-manifest.json')])
        merge_mirror_wrappers(ws, archive, candidate)
        run(candidate, log, ['npm', 'install'], cwd=candidate / 'astro-project')
        run(candidate, log, ['npm', 'run', 'build'], cwd=candidate / 'astro-project')
        # Never replace a delivery with fewer pages than it already had.
        import zipfile
        with zipfile.ZipFile(archive) as z:
            old = [n.split('/dist/', 1)[1] for n in z.namelist() if '/dist/' in n and n.endswith('.html')]
        if any(not astro.built_file(candidate / 'astro-project/dist', f) for f in old):
            raise RuntimeError('Candidate lost a previously delivered page')
        run(candidate, log, [sys.executable, HERE / 'verify-static.py', '--original', candidate / 'input-untouched',
                            '--dist', candidate / 'astro-project/dist', '--out', candidate / 'verify-static', '--jobs', '1'], allowed_failure=True)
        run(candidate, log, ['node', HERE / 'verify-parity.mjs', '--manifest=' + str(candidate / 'conversion-manifest.json')], allowed_failure=True)
        coverage = astro.coverage(candidate)
        write(candidate / 'astro-coverage.json', coverage)
        if coverage['missing'] or coverage['captureErrors'] or (read(candidate / 'prerender-report.json') or {}).get('failure') or ((read(candidate / 'prerender-report.json') or {}).get('coverage') or {}).get('passed') is False:
            raise RuntimeError('Candidate capture is still incomplete: ' + coverage['detail'])
        (candidate / 'CONVERSION-REPORT.md').write_text('# Astro recovery\n\n' + coverage['detail'] +
            '\n\nAn isolated serial capture, Astro build and fresh A/A2 checks were run.\n'
            'Source runtime errors and incomplete routes remain reported in result.json.\n')
        # The writer packages the candidate, not the previous workspace ZIP.
        run(candidate, log, [sys.executable, HERE / 'write-result.py', candidate, '--mode', 'astro'])
        result = read(candidate / 'out/result.json')
        artifact = candidate / 'out' / result['astro']['file']
        if not astro.valid_zip(artifact):
            raise RuntimeError('Candidate Astro ZIP is invalid')
        revision = int(base.get('revision') or 1) + 1
        export_name = f"{base['site']['slug']}-astro-{base['site'].get('version') or '1.0.0'}-r{revision}.zip"
        result['repairRequestId'] = turn
        result['revision'] = revision; result['checkedRevision'] = revision
        result['builtSite'] = {'path': str(ws / 'astro-project/dist'), 'workspacePath': 'astro-project/dist', 'index': 'index.html'} if (candidate / 'astro-project/dist/index.html').is_file() else None
        # Stage bookkeeping with the candidate: a publication rollback leaves
        # the real attempt open so the retained outcome can close it as failed.
        write(candidate / 'progress.json', read(ws / 'progress.json') or {})
        close_attempt(candidate / 'progress.json', candidate / 'result.json', '-1', 'fixed' if coverage['passed'] else 'failed', coverage['detail'])
        result['repairs'] = (read(candidate / 'progress.json') or {}).get('repairs', [])
        write(candidate / '.astro-applied.json', theme_state.astro_tree(candidate / 'astro-project'))
        changes = theme_state.load_changes(ws)
        changes.update(changedSinceZip=False, sinceZip=0,
                       lastZip={'file': export_name, 'revision': revision, 'at': theme_state.now()})
        write(candidate / 'changes.json', changes)
        import run_metadata
        result['execution'] = run_metadata.metadata(ws)
        out.mkdir(parents=True, exist_ok=True)
        publish_candidate(ws, out, history, candidate, result, export_name)
        print(result['verdict'] + '; previous ZIP kept; ' + export_name)
        return 0
    except Exception as error:
        progress = read(ws / 'progress.json') or {}
        if any(r.get('outcome') == 'open' for r in progress.get('repairs', [])):
            close_attempt(ws / 'progress.json', ws / 'result.json', '-1', 'failed', str(error))
        publish_retained(ws, out, base, str(error))
        print('Astro repair incomplete; previous ZIP retained: ' + str(error))
        return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('workspace', type=Path); ap.add_argument('action', choices=['repair'])
    a = ap.parse_args()
    try:
        return repair(a.workspace.resolve())
    except (RuntimeError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr); return 1


if __name__ == '__main__':
    sys.exit(main())
