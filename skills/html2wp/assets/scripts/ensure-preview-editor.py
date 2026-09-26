#!/usr/bin/env python3
"""Restore an installed editor in this workspace's own preview. No download."""
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from preview_owner import owned
from preview_editor import ensure


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--env', required=True)
    args = ap.parse_args()
    state = Path(args.env).absolute()
    workspace = Path(os.environ.get('H2WP_WORKSPACE') or state.parent).resolve()
    data = json.loads(state.read_text())
    if not owned(workspace, os.environ.get('H2WP_CONTAINER', ''), data.get('project', ''), state):
        raise RuntimeError('Visual Edit: preview ownership could not be verified')
    result = ensure(data['wpContainer'])
    print(json.dumps(result))
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
