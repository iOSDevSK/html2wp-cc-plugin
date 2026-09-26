#!/usr/bin/env python3
"""Run a repair's actual verification command and bind its exit to the attempt."""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'lib'))
from full_delivery import read, write, CHECK_REPORTS, verification_state
from repair_budget import input_digest


def main():
    if len(sys.argv) < 5 or sys.argv[3] != '--':
        sys.exit('usage: repair-check.py WORKSPACE STAGE -- command [args...]')
    ws, stage = Path(sys.argv[1]).resolve(), sys.argv[2]
    progress = read(ws / 'progress.json') or {}
    attempt = next((r for r in reversed(progress.get('repairs', []))
                    if r.get('stage') == stage and r.get('outcome') == 'open'), None)
    if not attempt and progress.get('mode') != 'full':
        sys.exit('no open repair for this check')
    # Verification is not another repair attempt. Full can check unaffected
    # stages without spending its owner's three editing attempts.
    attempt = attempt or {}
    allowed = {
        '-3': {'check-prereqs.sh'}, '-1': {'prerender-spa.py'},
        '0': {'check-manifest.py'}, '1': {'preflight-listings.mjs', 'verify-static.py'},
        '2': {'verify-static.py', 'verify-parity.mjs'}, '2.5': {'verify-parity.mjs'},
        '2.6': {'verify-static.py', 'verify-parity.mjs'}, '2.65': {'normalize-form-fields.py', 'verify-static.py'},
        '2.7': {'verify-parity.mjs'}, '3': {'make-zip.sh'}, '3.5': {'make-zip.sh'},
        '5': {'install-theme.py', 'quick-check.py', 'verify-wp.py', 'smoke-editor.py'},
        '5.5': {'verify-wp.py', 'smoke-editor.py'}, '5.6': {'audit-woo-coverage.py'},
        '6': {'make-zip.sh'},
    }
    command = sys.argv[4:]
    script_at = 1 if Path(command[0]).name in ('python', 'python3', Path(sys.executable).name, 'node', 'bash') else 0
    script = Path(command[script_at]).resolve() if len(command) > script_at else Path('/')
    if (script.parent != HERE or script.name not in allowed.get(stage, set())
            or any(arg.split('=')[0] in ('--help', '-h', '--version', '--dry-run', '--no-verify', '--threshold', '--variance', '--skip-editor', '--routes', '--only') for arg in command)):
        sys.exit('verification must run the installed stage checker, not an arbitrary successful command')
    before = input_digest(ws, attempt.get('files', []))
    checked_before = verification_state(ws, script.name)
    try:
        rc = subprocess.run(sys.argv[4:], cwd=ws, timeout=900, env={**os.environ, 'MAKE_ZIP_MANIFEST': str(ws / 'conversion-manifest.json'), 'MAKE_ZIP_BEST_EFFORT': '0'}).returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f'Check did not complete: {type(exc).__name__}', file=sys.stderr)
        rc = 124
    after = input_digest(ws, attempt.get('files', []))
    proof = {'attemptId': attempt.get('id'), 'exit': rc, 'checker': script.name,
             'inputs': after, 'stable': before == after,
             'turn': os.environ.get('H2WP_TURN'),
             'verificationInputs': verification_state(ws, script.name),
             'verificationStable': checked_before is not None and checked_before == verification_state(ws, script.name),
             'reports': {rel: hashlib.sha256((ws / rel).read_bytes()).hexdigest()
                         for rel in CHECK_REPORTS.get(script.name, ()) if (ws / rel).is_file()},
             'commandHash': hashlib.sha256('\0'.join(sys.argv[4:]).encode()).hexdigest()}
    write(ws / f'repair-check-{stage}.json', proof)
    history = read(ws / 'repair-checks.json') or {}
    history.setdefault(stage, {})[script.name] = proof
    write(ws / 'repair-checks.json', history)
    return rc


if __name__ == '__main__':
    sys.exit(main())
