#!/usr/bin/env python3
"""Restore missing images in a WORKING static HTML copy (Flash and Full).
Unresolved assets are reported, not a reason to withhold an otherwise usable ZIP.
"""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from source_assets import recover


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', required=True, help='working HTML copy, never the owner source')
    ap.add_argument('--project', help='read-only source project containing assets and .asset.json metadata')
    ap.add_argument('--asset-origin', default='', help='known original HTTPS origin; never guessed')
    ap.add_argument('--report', default='')
    args = ap.parse_args()
    root = Path(args.input)
    if root.is_symlink() or not root.is_dir():
        ap.error('--input must be a real directory')
    recover(root, args.project, args.asset_origin,
            args.report or root.parent / 'source-assets-report.json')
    return 0


if __name__ == '__main__':
    sys.exit(main())
