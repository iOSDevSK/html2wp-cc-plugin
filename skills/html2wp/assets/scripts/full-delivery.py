#!/usr/bin/env python3
"""Full HTML: defer a red quality check, retain a ZIP, or start an owner repair.

full-delivery.py WS defer --stage S --reason TEXT [--capture DIR]
full-delivery.py WS package
full-delivery.py WS begin
full-delivery.py WS fallback --reason TEXT

The server remains the theme builder. fallback only derives a manifest;
the agent then runs the ordinary build/upload/assembly/packaging stages.
"""
import argparse
import copy
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'lib'))
from full_delivery import read, write, record, valid_zip, usable_capture


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('workspace', type=Path)
    ap.add_argument('action', choices=['defer', 'package', 'begin', 'fallback', 'resolve', 'invalidate'])
    ap.add_argument('--stage', default='')
    ap.add_argument('--reason', default='')
    ap.add_argument('--capture', type=Path)
    a = ap.parse_args(argv)
    ws = a.workspace.resolve()
    p = read(ws / 'progress.json') or {}
    m = read(ws / 'conversion-manifest.json') or {}
    if p.get('mode') != 'full' or m.get('schema') == 'html2wp/2' or p.get('target') not in (None, '', 'html'):
        ap.error('this policy is for Full HTML runs only')
    if a.action == 'begin':
        if os.environ.get('H2WP_MODE') != 'repair-delivery' or not os.environ.get('H2WP_TURN'):
            ap.error('begin requires an owner repair-delivery turn')
        result = read(ws / 'result.json') or {}
        session = read(ws / 'repair-session.json') or {}
        turn = os.environ['H2WP_TURN']
        if session.get('turn') == turn:
            print('This repair turn already began; its budget is unchanged.')
            return 0
        if not result and session.get('base') and session.get('result', {}).get('status') == 'delivered':
            session['turn'] = turn
            write(ws / 'repair-session.json', session)
            p['state'] = 'running'
            write(ws / 'progress.json', p)
            print('Resumed interrupted repair with the new owner allowance; previous ZIP retained.')
            return 0
        if result.get('status') != 'delivered' or not (result.get('recovery') or {}).get('available'):
            ap.error('no delivered Full theme with outstanding checks')
        out = Path(os.environ.get('H2WP_OUTPUT_DIR') or ws / 'out')
        name = (result.get('theme') or {}).get('file', '')
        if not name or Path(name).name != name:
            ap.error('invalid delivered ZIP name')
        expected = (result.get('theme') or {}).get('sha256')
        source = next((d / name for d in (out, ws) if valid_zip(d / name)
                       and hashlib.sha256((d / name).read_bytes()).hexdigest() == expected), None)
        if source is None:
            ap.error('the previous valid ZIP must be retained before repair')
        history = ws / 'repair-history' / str(time.time_ns())
        history.mkdir(parents=True)
        shutil.copy2(source, history / name)
        write(history / 'result.json', result)
        # Preserve the editable state as well as the last release. Repairs may
        # rebuild dependencies, but owner changes must remain recoverable.
        if (ws / 'theme').is_dir():
            shutil.copytree(ws / 'theme', history / 'theme', symlinks=True)
        if (ws / 'conversion-manifest.json').exists():
            shutil.copy2(ws / 'conversion-manifest.json', history / 'conversion-manifest.json')
        # Keep capture provenance available to subsequent builds. Snapshot the
        # reports and fingerprint them so they cannot certify a new artifact.
        for rel in ('prerender-report.json', 'verify-static/report.json', 'parity-report.json',
                    'verify-parity/report.json', 'preflight-listings.json', 'install-theme/report.json',
                    'quick-check.json', 'verify-wp/report.json', 'smoke-editor/report.json', 'woo-coverage/report.json'):
            old = ws / rel
            if old.is_file():
                dest = history / 'checks' / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old, dest)

        # Preserve the old artifact and checks. A repair never erases an export.
        baseline = {str(f.relative_to(history / 'checks')): {'sha256': hashlib.sha256(f.read_bytes()).hexdigest(), 'mtime': (ws / f.relative_to(history / 'checks')).stat().st_mtime_ns} for f in (history / 'checks').rglob('*') if f.is_file()}
        write(ws / 'repair-session.json', {'turn': turn, 'base': str(history), 'result': result, 'reports': baseline})
        for path in (ws / 'result.json', out / 'result.json'):
            if path.exists():
                path.unlink()
        p['state'] = 'running'
        write(ws / 'progress.json', p)
        write(ws / 'repair-invalidated.json', {'stages': (result.get('recovery') or {}).get('stages', [])})
        print('Previous ZIP retained. Repair the listed issues; rebuild only invalidated dependencies, then deliver again.')
        return 0
    if a.action == 'invalidate':
        from repair_budget import input_digest
        changed = next((r for r in reversed(p.get('repairs', [])) if r.get('outcome') == 'open' and not r.get('invalidated') and r.get('beforeDigest') and r.get('beforeDigest') != input_digest(ws, r.get('files', []))), None)
        if not changed or not a.stage or not a.reason:
            ap.error('invalidate needs a changed counted repair, --stage and --reason')
        stages = [r['stage'] for r in p.get('stages', [])]
        if a.stage not in stages:
            ap.error('unknown dependency stage')
        write(ws / 'repair-invalidated.json', {'stages': stages[stages.index(a.stage):], 'reason': a.reason})
        changed['invalidated'] = True
        write(ws / 'progress.json', p)
        return 0
    if a.action == 'resolve':
        from repair_budget import input_digest
        row = next((r for r in reversed(p.get('repairs', []))
                    if r.get('stage') == a.stage and r.get('outcome') == 'fixed'), None)
        required = {'-1': {'prerender-spa.py'}, '0': {'check-manifest.py'},
                    '1': {'preflight-listings.mjs', 'verify-static.py'},
                    '2': {'verify-static.py', 'verify-parity.mjs'},
                    '2.5': {'verify-parity.mjs'},
                    '2.6': {'verify-static.py', 'verify-parity.mjs'},
                    '2.65': {'normalize-form-fields.py', 'verify-static.py'},
                    '2.7': {'verify-parity.mjs'},
                    '3': {'make-zip.sh'}, '3.5': {'make-zip.sh'},
                    '5': {'install-theme.py', 'quick-check.py', 'verify-wp.py', 'smoke-editor.py'},
                    '5.5': {'verify-wp.py', 'smoke-editor.py'},
                    '5.6': {'audit-woo-coverage.py'}, '6': {'make-zip.sh'}}.get(a.stage, set())
        checks = (read(ws / 'repair-checks.json') or {}).get(a.stage, {})
        if not row or not required or any(
            checks.get(name, {}).get('attemptId') != row.get('id') or checks[name].get('exit') != 0
            or checks[name].get('stable') is not True
            or checks[name].get('inputs') != input_digest(ws, row.get('files', [])) for name in required):
            ap.error('resolve needs all stage checkers on current inputs; one substep cannot clear a whole stage')
        from full_delivery import issues
        write(ws / 'delivery-issues.json', {'schema': 'h2wp-delivery-issues/1',
                                           'issues': [r for r in issues(ws) if r['stage'] != a.stage]})
        return 0
    if a.action == 'defer':
        if not a.stage or not a.reason:
            ap.error('defer needs --stage and --reason (quality failures only)')
        if a.stage in ('-4', '-3'):
            ap.error('licensing, security and missing prerequisites cannot be deferred')
        if a.stage == '-1':
            if not a.capture or not usable_capture(a.capture) or not (read(ws / 'prerender-report.json') or {}).get('routes'):
                ap.error('stage -1 needs a recorded capture with rendered body content, not an empty SPA shell')
        record(ws, a.stage, a.reason)
        stopped = read(ws / 'result.json') or {}
        if stopped.get('status') == 'stopped':
            if os.environ.get('H2WP_MODE') != 'repair-stop' or (stopped.get('stopped') or {}).get('stage') != a.stage:
                ap.error('a stopped result can be resumed only by its owner at its stopping stage')
            os.replace(ws / 'result.json', ws / f'result-stopped-{time.time_ns()}.json')
        return subprocess.call(['bash', str(HERE / 'progress.sh'), 'warn', a.stage, a.reason],
                               env={**os.environ, 'H2WP_WORKSPACE': str(ws)})
    if a.action == 'fallback':
        if not a.reason or not m:
            ap.error('fallback requires a manifest and a measured failure reason')
        if (ws / 'fallback-manifest.json').exists():
            ap.error('the static fallback was already prepared; do not loop')
        dist = ws / 'astro-project' / 'dist'
        pages = m.get('pages') or []
        for page in pages:
            source = (dist / page['file']).resolve()
            if not source.is_relative_to(dist.resolve()) or not source.is_file():
                ap.error('fallback needs every declared source page in the built dist')
        # Keep requested capabilities and content classification separate from
        # the explicitly degraded assembly plan. No source page is removed.
        write(ws / 'requested-manifest.json', m)
        fallback = copy.deepcopy(m)
        fallback['input'] = {**m.get('input', {}), 'dir': str(dist), 'type': 'built-dist'}
        fallback['chrome'] = {'frontOwnsFooter': True}
        fallback['nav'] = []
        fallback['declaredCollections'] = []
        fallback.pop('collections', None)
        fallback.pop('anchors', None)
        for key in ('blog', 'shop'):
            fallback[key] = {'present': False, 'reason': 'Static fallback; requested capability retained in requested-manifest.json'}
        for page in fallback['pages']:
            page['kind'] = 'front' if page['file'] == 'index.html' else 'page'
            page['chrome'] = 'self-contained'
        # The source input must outlive the stage-1 rebuild that replaces dist.
        capture = ws / 'fallback-input'
        shutil.copytree(dist, capture)
        fallback['input']['dir'] = str(capture)
        write(ws / 'fallback-manifest.json', fallback)
        write(ws / 'conversion-manifest.json', fallback)
        stages = [r['stage'] for r in p.get('stages', [])]
        write(ws / 'repair-invalidated.json', {'stages': stages[stages.index('1'):] if '1' in stages else []})
        record(ws, '3', 'Static fallback: dynamic blog/shop/menu wiring is incomplete. ' + a.reason)
        print('Static assembly plan prepared; run stage 1 and invalidated dependencies, then convert-remote through the licensed server. Keep requested-manifest.json in the report.')
        return 0
    slug = (m.get('site') or {}).get('slug', '')
    version = (m.get('site') or {}).get('version') or '1.0.0'
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,48}', slug) or not re.fullmatch(r'[A-Za-z0-9._-]+', version):
        ap.error('invalid theme identity')
    if (read(ws / '.h2wp-result.json') or {}).get('status') != 'SUCCESS':
        ap.error('package needs a successful server assembly in this run')
    theme = ws / 'theme' / slug
    dest = ws / f'{slug}-{version}.zip'
    tmp = ws / f'.{slug}-delivery.zip'
    packed = subprocess.run(['bash', str(HERE / 'make-zip.sh'), str(theme), str(tmp)],
                            env={**os.environ, 'MAKE_ZIP_MANIFEST': str(ws / 'conversion-manifest.json'),
                                 'MAKE_ZIP_BEST_EFFORT': '1', 'H2WP_WORKSPACE': str(ws)})
    if packed.returncode or not valid_zip(tmp):
        tmp.unlink(missing_ok=True)
        print('No new installable ZIP; retain the last valid ZIP and repair assembly.', file=sys.stderr)
        return 1
    os.replace(tmp, dest)
    write(ws / 'delivery-artifact.json', {'turn': os.environ.get('H2WP_TURN'),
          'file': dest.name, 'sha256': hashlib.sha256(dest.read_bytes()).hexdigest()})
    print(dest)
    return 0


if __name__ == '__main__':
    sys.exit(main())
