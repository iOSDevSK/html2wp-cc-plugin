#!/usr/bin/env python3
"""Reproducible, allowlisted service upload for both manifest generations.

Stable archive bytes are required for client retries to reuse their saved job.
BSD tar metadata and gzip timestamps otherwise change an unchanged upload.
"""
import gzip
from pathlib import Path, PurePosixPath
import sys
import tarfile


def pack(workspace, destination, members):
    workspace = Path(workspace).resolve()
    visited = set()
    def paths(name):
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name:
            raise ValueError('upload member must be a relative workspace path')
        path = workspace / name
        # Excluded before the symlink check: node_modules may itself be a link
        # (pnpm, a shared install) and is never shipped either way.
        if any(part in ('node_modules', '.astro', '.DS_Store') or part.startswith('._') for part in relative.parts):
            return
        if any(part.is_symlink() for part in [path, *list(path.parents)[:len(relative.parts)-1]]):
            raise ValueError('upload cannot contain symlinks: ' + name)
        if name in visited:
            return
        visited.add(name)
        if not path.is_dir() and not path.is_file():
            raise ValueError('upload member is missing or not a regular file: ' + name)
        yield name, path
        if path.is_dir():
            for child in sorted(path.iterdir(), key=lambda p: p.name):
                yield from paths(relative.joinpath(child.name).as_posix())
    with open(destination, 'wb') as output:
        with gzip.GzipFile(filename='', fileobj=output, mode='wb', mtime=0, compresslevel=6) as compressed:
            with tarfile.open(fileobj=compressed, mode='w|', format=tarfile.PAX_FORMAT, dereference=True) as archive:
                for member in sorted(members):
                    for name, path in paths(member):
                        info = archive.gettarinfo(str(path), arcname=name)
                        info.uid = info.gid = info.mtime = 0
                        info.uname = info.gname = ''
                        info.pax_headers = {}
                        info.mode = 0o755 if info.isdir() else 0o644
                        if info.isdir():
                            archive.addfile(info)
                        else:
                            with path.open('rb') as source:
                                archive.addfile(info, source)


if __name__ == '__main__':
    try:
        if len(sys.argv) < 4:
            raise ValueError('usage: gutenberg-pack-upload.py WORKSPACE ARCHIVE MEMBER...')
        pack(sys.argv[1], sys.argv[2], sys.argv[3:])
    except (ValueError, OSError) as error:
        print('upload pack: ' + str(error), file=sys.stderr)
        sys.exit(1)
