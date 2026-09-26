#!/usr/bin/env python3
"""Record and reapply local HTML theme repairs, retaining old artifacts on conflict."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from theme_patches import Patches, PatchError


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('workspace', type=Path)
    ap.add_argument('action', choices=['begin', 'record', 'abort', 'capture', 'integrate', 'resolve', 'check', 'status'])
    ap.add_argument('--stage', default='5')
    ap.add_argument('--reason', default='')
    ap.add_argument('--theme', type=Path)
    a = ap.parse_args()
    try:
        p = Patches(a.workspace)
        with p.locked():
            p.recover()
            if a.action in ('begin', 'resolve'):
                if not a.reason.strip(): raise PatchError('--reason must explain the diagnosed repair')
                getattr(p, a.action)(a.stage, a.reason)
            elif a.action == 'integrate':
                if not a.theme: raise PatchError('integrate needs the staged --theme directory')
                if a.theme.name != p.slug or a.theme.parent.name != 'theme':
                    raise PatchError('staged theme identity does not match the manifest')
                p.integrate(a.theme)
            elif a.action == 'status':
                state = p.load() or {}
                conflict = state.get('conflict') or {}
                print(json.dumps({'id': state.get('id'), 'events': [{k: v for k, v in e.items() if k != 'changes'} | {'files': list(e['changes'])}
                                             for e in state.get('events', [])],
                                  'pending': bool(state.get('pending')),
                                  'conflict': {'files': conflict.get('files', []), 'directory': conflict.get('directory'),
                                               'versions': {name: {'base': state.get('base', {}).get(name),
                                                                   'local': state.get('expected', {}).get(name),
                                                                   'server': conflict.get('remote', {}).get(name)}
                                                            for name in conflict.get('files', [])}}}))
            else:
                result = getattr(p, a.action)()
                if a.action == 'capture':
                    print(json.dumps({'events': len(result.get('events', []))}))
                elif result is not None: print(json.dumps(result))
        return 0
    except (PatchError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print('theme-patches: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
