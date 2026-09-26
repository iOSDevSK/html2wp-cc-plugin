"""Persistent, transactional local theme patches; never executes patch content."""
import contextlib
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import time

from full_delivery import read, write


class PatchError(Exception):
    pass


def safe_name(name):
    p = PurePosixPath(name)
    if not name or p.is_absolute() or '..' in p.parts or '\\' in name or str(p) != name:
        raise PatchError('unsafe theme path')
    return name


class Patches:
    def __init__(self, workspace):
        self.ws = Path(workspace).resolve()
        self.manifest = read(self.ws / 'conversion-manifest.json') or {}
        self.slug = (self.manifest.get('site') or {}).get('slug', '')
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,48}', self.slug):
            raise PatchError('invalid theme slug')
        if self.manifest.get('schema') == 'html2wp/2' or self.manifest.get('target') == 'gutenberg':
            raise PatchError('theme patches currently support HTML themes only')
        self.theme = self.ws / 'theme' / self.slug
        self.root = self.ws / 'theme-patches'
        for path in (self.root, self.ws / 'theme', self.theme):
            if path.is_symlink():
                raise PatchError('theme and patch storage must not be symlinks')
        self.root.mkdir(exist_ok=True)
        if any(p.is_symlink() for p in self.root.iterdir()):
            raise PatchError('patch metadata must not be symlinks')
        self.blobs = self.root / 'blobs'
        if self.blobs.is_symlink():
            raise PatchError('patch blobs must not be symlinks')
        self.blobs.mkdir(exist_ok=True)

    @contextlib.contextmanager
    def locked(self):
        lock = self.root / 'lock'
        if lock.is_symlink():
            raise PatchError('unsafe patch lock')
        with lock.open('a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    def load(self):
        path = self.root / 'state.json'
        if not path.exists(): return None
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise PatchError('existing patch ledger is unreadable; restore its backup, never reinitialize it') from exc
        if not isinstance(state, dict) or not isinstance(state.get('base'), dict) or not isinstance(state.get('expected'), dict) or not isinstance(state.get('events'), list):
            raise PatchError('invalid patch ledger structure')
        if state and (state.get('slug') != self.slug or state.get('schema') != 'h2wp-theme-patches/1'):
            raise PatchError('patch ledger belongs to another theme')
        return state

    def blob(self, data):
        sha = hashlib.sha256(data).hexdigest()
        dest = self.blobs / sha
        if dest.is_symlink():
            raise PatchError('unsafe patch blob')
        if not dest.exists():
            dest.write_bytes(data)
        return sha

    def data(self, sha):
        if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha):
            raise PatchError('invalid patch blob identity')
        path = self.blobs / sha
        if path.is_symlink():
            raise PatchError('unsafe patch blob')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise PatchError('corrupt patch blob')
        return data

    def snapshot(self, theme):
        theme = Path(theme)
        if theme.is_symlink() or not theme.is_dir():
            raise PatchError('no regular theme directory')
        result = {}
        for f in sorted(theme.rglob('*')):
            if f.is_symlink() or not (f.is_file() or f.is_dir()):
                raise PatchError('theme contains a link or special file')
            if f.is_file():
                name = safe_name(f.relative_to(theme).as_posix())
                result[name] = self.blob(f.read_bytes())
        if not result:
            raise PatchError('empty theme')
        return result

    def materialize(self, files, directory):
        directory.mkdir(parents=True, exist_ok=True)
        for name, sha in files.items():
            path = directory / safe_name(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.data(sha))

    def validate(self, files):
        for name in ('style.css', 'theme.json', 'functions.php'):
            if name not in files:
                raise PatchError('patch removed required theme file: ' + name)
        if 'templates/index.html' not in files and 'index.php' not in files:
            raise PatchError('patch removed the index template')
        if not isinstance(json.loads(self.data(files['theme.json'])), dict):
            raise PatchError('invalid theme.json')
        for name, sha in files.items():
            if name.endswith('.php'):
                run = subprocess.run(['php', '-l'], input=self.data(sha), capture_output=True, timeout=30)
                if run.returncode:
                    raise PatchError('PHP syntax error in ' + name)

    def save(self, state):
        write(self.root / 'state.json', state)

    def initialize(self):
        state = self.load()
        if not state:
            current = self.snapshot(self.theme)
            state = {'schema': 'h2wp-theme-patches/1', 'id': str(time.time_ns()), 'slug': self.slug, 'base': current,
                     'expected': current, 'events': [], 'legacyUnknown': True}
            self.save(state)
        return state

    def event(self, state, before, after, reason, stage=None, attempt=None):
        changes = {p: {'before': before.get(p), 'after': after.get(p)}
                   for p in sorted(before.keys() | after.keys()) if before.get(p) != after.get(p)}
        if not changes:
            return None
        eid = str(time.time_ns())
        lines = []
        for name, change in changes.items():
            a = self.data(change['before']) if change['before'] else b''
            b = self.data(change['after']) if change['after'] else b''
            try:
                if b'\0' in a + b: raise UnicodeError()
                lines.extend(difflib.unified_diff(a.decode().splitlines(True), b.decode().splitlines(True),
                                                 fromfile='a/' + name, tofile='b/' + name))
            except UnicodeError:
                lines.append(f'Binary file {name}: {change["before"]} -> {change["after"]}\n')
        (self.root / (eid + '.patch')).write_text(''.join(lines))
        state['events'].append({'id': eid, 'reason': reason, 'stage': stage, 'attempt': attempt,
                                'changes': changes, 'diff': eid + '.patch'})
        return eid

    def authorization(self, stage):
        progress = read(self.ws / 'progress.json') or {}
        result = read(self.ws / 'result.json') or {}
        if result.get('status') == 'delivered' and os.environ.get('H2WP_MODE') != 'repair-delivery':
            return None
        row = next((r for r in reversed(progress.get('repairs', []))
                    if r.get('stage') == stage and r.get('outcome') == 'open'), None)
        if not row:
            raise PatchError('open a counted repair for this stage before editing the theme')
        return row.get('id')

    def capture(self, reason='Preserved local changes before rebuild'):
        state = self.initialize()
        if state.get('pending') or state.get('conflict'):
            raise PatchError('finish or abort the pending patch/conflict before rebuilding')
        current = self.snapshot(self.theme)
        self.validate(current)
        # The generated thumbnail is refreshed by stage 3.5, not a site repair.
        # Do not turn that normal refresh into a binary replay conflict.
        name = 'screenshot.png'
        if state['expected'].get(name) != current.get(name):
            state['base'] = dict(state['base'])
            state['expected'] = dict(state['expected'])
            for key in ('base', 'expected'):
                if name in current: state[key][name] = current[name]
                else: state[key].pop(name, None)
        if current != state['expected']:
            progress = read(self.ws / 'progress.json') or {}
            delivered = (read(self.ws / 'result.json') or {}).get('status') == 'delivered'
            stage = attempt = None
            if progress.get('mode') == 'full' and (not delivered or os.environ.get('H2WP_MODE') == 'repair-delivery'):
                row = next((r for r in reversed(progress.get('repairs', [])) if r.get('outcome') == 'open'), None)
                if not row:
                    raise PatchError('unrecorded Full theme changes need an open counted repair before capture or packaging')
                stage, attempt = row.get('stage'), row.get('id')
            self.event(state, state['expected'], current, reason, stage, attempt)
        state['expected'] = current
        self.save(state)
        return state

    def begin(self, stage, reason):
        attempt = self.authorization(stage)
        state = self.capture('Preserved existing edits before local repair')
        state['pending'] = {'before': state['expected'], 'stage': stage, 'reason': reason, 'attempt': attempt}
        self.save(state)

    def record(self):
        state = self.load() or {}
        pending = state.get('pending')
        if not pending:
            raise PatchError('no pending theme patch')
        if self.authorization(pending['stage']) != pending['attempt']:
            raise PatchError('the patch must finish in the repair attempt that opened it')
        current = self.snapshot(self.theme)
        self.validate(current)
        eid = self.event(state, pending['before'], current, pending['reason'], pending['stage'], pending['attempt'])
        if not eid:
            raise PatchError('no theme change to record; abort the empty patch')
        state['expected'] = current
        state.pop('pending')
        self.save(state)
        return eid

    def received(self, incoming):
        reports = {}
        for name in ('theme-report.json', 'chrome-groups.json'):
            path = Path(incoming).parent.parent / name
            if path.is_symlink(): raise PatchError('server report must not be a symlink')
            if path.exists():
                report = json.loads(path.read_text())
                if not isinstance(report, dict): raise PatchError('invalid server report: ' + name)
                reports[name] = report
        return reports

    def apply_received(self, state):
        reports = state.get('received') or {}
        for name in ('theme-report.json', 'chrome-groups.json'):
            if name in reports: write(self.ws / name, reports[name])
        report = reports.get('theme-report.json') or {}
        manifest = read(self.ws / 'conversion-manifest.json') or {}
        nav = manifest.get('nav') if isinstance(manifest.get('nav'), list) else []
        changed = False
        for zone in report.get('menusDeclared') or []:
            match = re.search(r'_nav_(\d+)$', str(zone.get('location') or ''))
            if match and isinstance(zone.get('selector'), str):
                i = int(match.group(1)) - 1
                if 0 <= i < len(nav) and isinstance(nav[i], dict):
                    nav[i]['zoneSelector'] = zone['selector']
                    nav[i].pop('unwired', None)
                    changed = True
        for entry in report.get('menusUnwired') or []:
            i = entry.get('index')
            if isinstance(i, int) and 0 <= i < len(nav) and isinstance(nav[i], dict):
                nav[i]['unwired'] = str(entry.get('reason') or 'menu not editable, static nav kept')
                nav[i].pop('zoneSelector', None)
                changed = True
        if changed: write(self.ws / 'conversion-manifest.json', manifest)
        if state.get('resolvedConflict'):
            service = read(self.ws / '.h2wp-result.json') or {}
            if service.get('code') == 'LOCAL_PATCH_CONFLICT':
                service.update(status='SUCCESS', code='LOCAL_PATCH_RESOLVED', stage='done',
                               message='Server assembly retained; local patch conflict resolved. Verification required.')
                write(self.ws / '.h2wp-result.json', service)

    def promote(self, files, state, receive=False, expected_live=False):
        """Journal first: interrupted swaps are recovered before another action."""
        target = self.root / 'promotion'
        if target.exists(): shutil.rmtree(target)
        self.materialize(files, target)
        if expected_live is not False and (self.snapshot(self.theme) if self.theme.exists() else None) != expected_live:
            raise PatchError('working theme changed during replay; no promotion performed')
        backup = self.root / ('theme-backup-' + str(time.time_ns()))
        journal = {'backup': backup.name, 'state': state, 'receive': receive}
        write(self.root / 'promotion.json', journal)
        self.theme.parent.mkdir(exist_ok=True)
        if self.theme.exists(): os.replace(self.theme, backup)
        os.replace(target, self.theme)
        if receive: self.apply_received(state)
        self.save(state)
        (self.root / 'promotion.json').unlink()

    def recover(self):
        journal = read(self.root / 'promotion.json')
        if not journal: return
        backup = self.root / safe_name(journal['backup'])
        target = self.root / 'promotion'
        if target.exists():
            # Swap had not finished; restore old working tree, leave new build for retry.
            if not self.theme.exists() and backup.exists(): os.replace(backup, self.theme)
            shutil.rmtree(target)
        elif self.theme.exists():
            if journal.get('receive'): self.apply_received(journal['state'])
            self.save(journal['state'])
        else:
            raise PatchError('incomplete theme promotion; retained backup requires inspection')
        (self.root / 'promotion.json').unlink()

    def abort(self):
        state = self.load() or {}
        pending = state.get('pending')
        if not pending:
            raise PatchError('no pending patch to abort')
        state.pop('pending')
        state['expected'] = pending['before']
        self.promote(pending['before'], state)

    def merge(self, base, local, remote):
        if remote == local or local == base: return remote, False
        if remote == base: return local, False
        # Adds, deletions and binary changes require explicit conflict resolution.
        if None in (base, local, remote): return local, True
        data = [self.data(x) for x in (local, base, remote)]
        if any(b'\0' in x for x in data): return local, True
        try:
            for x in data: x.decode('utf-8')
        except UnicodeError:
            return local, True
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            paths = [Path(tmp) / str(i) for i in range(3)]
            for path, content in zip(paths, data): path.write_bytes(content)
            run = subprocess.run(['git', 'merge-file', '-p', *map(str, paths)], capture_output=True, timeout=30)
            if run.returncode == 0: return self.blob(run.stdout), False
        return local, True

    def integrate(self, incoming):
        had_theme = self.theme.exists()
        state = self.capture() if self.theme.exists() else self.load()
        if state and (state.get('pending') or state.get('conflict')):
            raise PatchError('resolve the outstanding patch before integrating')
        remote = self.snapshot(incoming)
        received = self.received(incoming)
        self.validate(remote)
        if not state:
            state = {'schema': 'h2wp-theme-patches/1', 'id': str(time.time_ns()), 'slug': self.slug, 'base': remote,
                     'expected': remote, 'events': []}
        base, local = state['base'], state['expected']
        merged = dict(remote)
        conflicts = []
        unknown = state.get('legacyUnknown') and remote != base
        if unknown:
            # A legacy working tree is not evidence of pristine server output.
            # Preserve it intact until a counted explicit reconciliation.
            merged = dict(local)
            conflicts = sorted(p for p in remote.keys() | local.keys() if remote.get(p) != local.get(p))
        for path in ([] if unknown else sorted(base.keys() | local.keys())):
            if base.get(path) == local.get(path): continue
            value, conflict = self.merge(base.get(path), local.get(path), remote.get(path))
            if conflict: conflicts.append(path)
            if value is None: merged.pop(path, None)
            else: merged[path] = value
        if conflicts:
            candidate = self.root / ('conflict-' + str(time.time_ns()))
            self.materialize(merged, candidate)
            state['conflict'] = {'directory': candidate.name, 'files': conflicts,
                                 'remote': remote, 'received': received,
                                 'live': self.snapshot(self.theme) if self.theme.exists() else None}
            self.save(state)
            raise PatchError('patch conflicts: ' + ', '.join(conflicts) + '; edit candidate at ' + str(candidate))
        self.validate(merged)
        self.event(state, remote, merged, 'Reapplied local patches to server theme')
        # An already patched candidate must not erase the original precondition
        # (A→B→C replayed on C must still replay when the server later sends A).
        rebased = dict(remote)
        for path in base.keys() | local.keys():
            if local.get(path) != base.get(path) and remote.get(path) == local.get(path):
                if base.get(path) is None: rebased.pop(path, None)
                else: rebased[path] = base[path]
        state.update(base=rebased, expected=merged, received=received)
        state.pop('legacyUnknown', None)
        state.pop('resolvedConflict', None)
        self.promote(merged, state, receive=True, expected_live=local if had_theme else None)

    def resolve(self, stage, reason):
        attempt = self.authorization(stage)
        state = self.load() or {}
        conflict = state.get('conflict')
        if not conflict: raise PatchError('no rebuild conflict')
        if (self.snapshot(self.theme) if self.theme.exists() else None) != conflict['live']:
            raise PatchError('working theme changed during conflict resolution; preserve those edits before retrying')
        candidate = self.root / safe_name(conflict['directory'])
        files = self.snapshot(candidate)
        for name, sha in files.items():
            if re.search(rb'(?m)^(<<<<<<< |=======\r?$|>>>>>>> )', self.data(sha)):
                raise PatchError('unresolved conflict marker in ' + name)
        self.validate(files)
        self.event(state, conflict['remote'], files, reason, stage, attempt)
        state.update(base=conflict['remote'], expected=files, received=conflict.get('received', {}), resolvedConflict=True)
        state.pop('legacyUnknown', None)
        state.pop('conflict')
        self.promote(files, state, receive=True, expected_live=conflict['live'])

    def check(self):
        state = self.load()
        if state and (state.get('pending') or state.get('conflict')):
            raise PatchError('theme patch is pending or conflicting; retain the last ZIP and finish the patch')
        return {'events': len((state or {}).get('events', [])),
                'pending': bool((state or {}).get('pending')), 'conflict': (state or {}).get('conflict', {}).get('files', [])}
