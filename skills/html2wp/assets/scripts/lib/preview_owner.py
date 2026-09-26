"""Preview ownership is a workspace AND runtime identity, never a theme slug.

A logical /project/workspace path can name many different app projects.
Unknown legacy orphans are deliberately left alone, not garbage-collected.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def identity(workspace, container=''):
    return hashlib.sha256(json.dumps([str(Path(workspace).resolve()), container],
                                    separators=(',', ':')).encode()).hexdigest()


def docker(*args):
    done = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=20)
    if done.returncode:
        raise ValueError('Docker could not prove preview ownership')
    return done.stdout


def owned(workspace, container, project, state_path=None):
    if not re.fullmatch(r'h2wp-[a-z0-9-]+-[0-9a-f]{6}', project) or 'clara-test' in project:
        return False
    expected = identity(workspace, container)
    state = None
    if state_path and Path(state_path).is_file():
        path = Path(state_path)
        if path.is_symlink() or path.parent.resolve() != Path(workspace).resolve():
            return False
        state = json.loads(path.read_text())
        if not isinstance(state, dict) or state.get('project') != project:
            return False
        if state.get('owner') and state['owner'] != expected:
            return False
        slug = state.get('slug', '')
        if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9-]+', slug):
            return False
        if not re.fullmatch(re.escape('h2wp-' + slug + '-') + r'[0-9a-f]{6}', project):
            return False
        if path.name != '.test-env-' + slug + '.json' or state.get('network', project + '_default') != project + '_default':
            return False
    ids = docker('ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project).split()
    if state:
        for key, service in (('wpContainer', 'wp'), ('dbContainer', 'db')):
            name = state.get(key)
            if name not in (project + '-' + service + '-1', project + '_' + service + '_1'):
                return False
            # A recorded name may exist under a DIFFERENT compose label.
            # Inspect it independently even if the expected project is empty.
            for ident in docker('ps', '-aq', '--filter', 'name=^/' + re.escape(name) + '$').split():
                if ident not in ids:
                    ids.append(ident)
    resource_labels = []
    for kind in ('volume', 'network'):
        names = docker(kind, 'ls', '-q', '--filter', 'label=com.docker.compose.project=' + project).split()
        if names:
            resource_labels.extend(json.loads(line) or {} for line in docker(
                kind, 'inspect', '--format', '{{json .Labels}}', *names).splitlines() if line.strip())
    if any(l.get('h2wp.owner') and l['h2wp.owner'] != expected for l in resource_labels):
        return False
    if not ids:
        # Recreate an absent preview only from new-format, matching state,
        # and only if any surviving volumes/networks also prove ownership.
        return bool(state and state.get('owner') == expected
                    and all(l.get('h2wp.owner') == expected for l in resource_labels))
    rows = [json.loads(line) for line in docker('inspect', '--format',
            '{"name":{{json .Name}},"labels":{{json .Config.Labels}}}', *ids).splitlines() if line.strip()]
    if len(rows) != len(ids):
        return False
    labels = [r.get('labels') or {} for r in rows]
    if any(l.get('com.docker.compose.project') != project for l in labels):
        return False
    names = {r.get('name', '').lstrip('/') for r in rows}
    if state and any(state[k] not in names for k in ('wpContainer', 'dbContainer')) and state.get('owner') != expected:
        return False  # partial recovery requires a modern matching-owner state
    owners = [l.get('h2wp.owner', '') for l in labels]
    if any(owners):
        return all(o == expected for o in owners)

    # Legacy CLI labels with a canonical, host-unique path can still prove
    # ownership. Container-local aliases cannot, even when strings match.
    expected_state = str(Path(state_path).resolve()) if state_path else ''
    paths = [l.get('h2wp.state', '') for l in labels]
    if not state:
        return False  # an unlabeled legacy orphan is never automatically removed
    if not container:
        return bool(expected_state and not expected_state.startswith('/project/')
                    and all(p == expected_state for p in paths))
    if not state or (state.get('relay') or {}).get('container') != container:
        return False
    if not all(p and Path(p).resolve() == Path(expected_state) for p in paths):
        return False
    # Network attachment is recoverable state: check/relay must be able to
    # reconnect after the agent container was restarted or recreated.
    return True


def available(project):
    if not re.fullmatch(r'h2wp-[a-z0-9-]+-[0-9a-f]{6}', project) or 'clara-test' in project:
        return False
    if docker('ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project).strip():
        return False
    return all(not docker(kind, 'ls', '-q', '--filter',
                          'label=com.docker.compose.project=' + project).strip()
               for kind in ('volume', 'network'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['identity', 'owns', 'available'])
    ap.add_argument('--workspace', required=True)
    ap.add_argument('--container', default=os.environ.get('H2WP_CONTAINER', ''))
    ap.add_argument('--project', default='')
    ap.add_argument('--state', default='')
    args = ap.parse_args()
    if args.action == 'identity':
        print(identity(args.workspace, args.container))
        return 0
    try:
        if args.action == 'available':
            return 0 if available(args.project) else 1
        return 0 if owned(args.workspace, args.container, args.project, args.state or None) else 1
    except (ValueError, OSError, subprocess.SubprocessError):
        print('test-env: ownership could not be verified; no preview resources will be changed', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
