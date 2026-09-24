#!/usr/bin/env python3
"""The HTML finishing pass after a Flash conversion: rounds, their bound, and
the ledger (SKILL.md "HTML Flash: the finishing pass").

  html-finish.py may-run --workspace WS --phase F1|F2|R [--history H]   (alias: rerun)
  html-finish.py round   --workspace WS --phase F1|F2|R [--history H] [--verify P] [--smoke P] [--woo P]
  html-finish.py round   --workspace WS --phase F1 --nothing-to-declare [--history H]
  html-finish.py restore --workspace WS --round N [--history H]
  html-finish.py end     --workspace WS [--round N] [--restore] [--history H]
  html-finish.py best    --workspace WS [--history H]
  html-finish.py ledger  --workspace WS [--history H] [--out P] [--row ID --cause converter-gap|source-ambiguity|owner-choice
                                                        --lever live-fix|manifest|report --note TEXT]
  html-finish.py status  --workspace WS [--history H]

A round is one run of the HTML gates (verify-wp, the editor smoke, the Woo
audit for a shop) on the state the agent's edits reached:
  F1, the manifest phase: blog, shop, collections, nav, set by the manifest
     and rebuilt; it ends when no structural row fails on a REBUILT manifest
     (the pass's first round measures HTML Flash's own), or when the agent
     states the site read found nothing to declare (--nothing-to-declare:
     no gate run, an uncounted entry the host appends like a round);
  F2, the theme phase: the theme-file residue, on the live-fix rails;
  R, a review round: the final check after the owner's live fixes, outside
     the pass's pool (the owner asked for it), its ledger the same.
The bound (the convergence guard's rules): a POOL of 4 counted gate runs, at
most 2 manifest rounds in F1; rule A, no run on a state a failing round
already measured; rule B, a round whose failing set is not strictly smaller
than the best so far OF THE SAME MANIFEST stops the pass (a rebuild measures
a new site, so its first round is a new baseline: articlePart appears once
articles exist); rule C, 90 minutes. A run that dies before it produces rows
does not count. On a stop the best round is delivered: of the latest manifest
whose structure came out green, the round with the fewest failing rows (ties
to the later), never an earlier manifest's round; with a ledger that says,
for every row still failing, the route, what differs, the cause and the
lever.

The round history is the host's when --history names it (the app keeps it
where the agent cannot write, and appends each `round` result's "round" to
it); without it the history is {workspace}/html-finish.json, kept here. The
ledger's causes and levers (the agent's judgment) are always in the
workspace, html-finish-notes.json; the ledger itself is html-finish-ledger.json.
Output is one JSON object; exit 1 only on a refusal.
"""
import argparse
import hashlib
import importlib.util
import io
import contextlib
import json
from pathlib import Path
import sys
import time

SCHEMA = 'h2wp-html-finish/1'
STATE = 'html-finish.json'
NOTES = 'html-finish-notes.json'
LEDGER = 'html-finish-ledger.json'
SNAPSHOTS = '.html-finish/manifests'
REPORTS = '.html-finish/reports'
REPORT_HOMES = {'verify': 'verify-wp/report.json', 'smoke': 'smoke-editor/report.json', 'woo': 'woo-coverage/report.json'}
POOL, F1_MAX, BACKSTOP = 4, 2, 90 * 60
CAUSES = ('converter-gap', 'source-ambiguity', 'owner-choice')
# The editor steps a manifest decides: the menu zones (nav[]). Every other
# editor step measures theme files (parts, page sources, the article part, the
# preview), which F1 may not touch and F2 fixes: kind "theme", never holding
# the pass in F1.
EDITOR_WIRING = frozenset({'menus', 'frontMenuPanel'})
LEVERS = ('live-fix', 'manifest', 'report')
HERE = Path(__file__).resolve().parent

spec = importlib.util.spec_from_file_location('h2wp_html_finish_live', HERE / 'live-fix.py')
LIVE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(LIVE)


class Refused(Exception):
    def __init__(self, code, message, lever):
        super().__init__(message)
        self.row = {'code': code, 'message': message, 'lever': lever}


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def stamp():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class History:
    """The rounds so far: the host's file (read only here), or the workspace's."""

    def __init__(self, ws, path=None):
        self.ws, self.host = Path(ws), bool(path)
        if path:
            value = read(path, [])
            self.rounds = value.get('rounds', []) if isinstance(value, dict) else value
            self.started = None  # the host keeps the time
        else:
            state = read(self.ws / STATE) or {'schema': SCHEMA, 'startedAt': stamp(), 'started': time.time(), 'rounds': []}
            if state.get('schema') != SCHEMA:
                raise Refused('state', f'{STATE} has an unknown schema', 'Restore it, or remove it to start the pass again.')
            self.state, self.rounds, self.started = state, state['rounds'], state['started']

    def append(self, entry, verdict):
        if not self.host:
            self.rounds.append(entry)
            self.state['verdict'] = verdict
            write(self.ws / STATE, self.state)

    def verdict(self):
        return None if self.host else self.state.get('verdict')

    def counted(self, review=True):
        return [r for r in self.rounds if r.get('counted') and (review or r['phase'] != 'R')]

    def best(self, extra=None):
        """The state delivered: after a review round, that round (the owner's
        fix made it); otherwise the pass's best round (best_of)."""
        rounds = [r for r in self.rounds + ([extra] if extra else []) if r.get('counted')]
        if rounds and rounds[-1]['phase'] == 'R':
            return rounds[-1]
        return best_of([r for r in rounds if r['phase'] != 'R'])


def best_of(rounds):
    """Of the pass's counted rounds, the one delivered: among the rounds of
    the latest manifest whose structure came out green, the fewest failing
    rows (a tie to the later). A wired blog is never traded back for the
    Flash build because wiring made a new row appear. With no green
    structure at all, the fewest failing rows of any round."""
    green = [r for r in rounds if not r['structural']]
    pool = [r for r in rounds if r.get('manifestDigest') == green[-1].get('manifestDigest')] if green else rounds
    return min(reversed(pool), key=lambda r: len(r['failing'])) if pool else None


def flash_manifest(rounds):
    """The manifest digest the pass started from: its first entry's, the
    round measured on HTML Flash's own build."""
    return next((r.get('manifestDigest') for r in rounds if r.get('manifestDigest')), None)


def rebuilt(rounds, entry):
    """Whether a rebuild is behind this round: its manifest is not the one
    the pass started from."""
    first = flash_manifest(rounds)
    return bool(first) and entry.get('manifestDigest') != first


def declared(rounds, entry):
    """Whether the agent stated, for this round, that the site read found
    nothing to declare (the uncounted entry --nothing-to-declare appends)."""
    return any(r.get('declared') == 'nothing' and r.get('of') == entry.get('n') for r in rounds)


def f1_done(rounds, entry):
    """F1 is over on this round: no structural row, and a rebuilt manifest
    or the explicit statement that there was nothing to declare."""
    return bool(entry) and not entry['structural'] and (rebuilt(rounds, entry) or declared(rounds, entry))


def digests(ws):
    """What a round measured: the theme's files and the manifest's decisions."""
    lay = LIVE.layout(ws)
    manifest = read(lay['ws'] / 'conversion-manifest.json') or {}
    theme, decisions = LIVE.digest(LIVE.file_hashes(lay['theme'])), LIVE.canonical(manifest)
    return {'state': hashlib.sha256(f'{theme}\0{decisions}'.encode()).hexdigest(), 'themeDigest': theme,
            'manifestDigest': decisions}, lay, manifest


def rule_a(rounds, now):
    for r in rounds:
        if r.get('counted') and r['failing'] and r['state'] == now['state']:
            raise Refused('unchanged', f'Round {r["n"]} already measured this state and it failed',
                          'Change something first (rule A: no rerun on an unchanged failing state).')


def may_run(args):
    """Whether one more gate run fits the bound; refused with the lever otherwise."""
    ws = Path(args.workspace).resolve()
    history = History(ws, args.history)
    now, _, _ = digests(ws)
    if not history.host and not (ws / STATE).is_file():
        write(ws / STATE, history.state)  # the pass's clock starts at its first question
    if args.phase == 'R':
        # A review round is the final check of an owner's live fix (the host
        # starts it at the owner's request): one per changed state.
        if not live_fixes(ws):
            raise Refused('review', 'A review round checks an owner\'s live fix, and no fix is recorded in the open cycle',
                          'Record the owner\'s fix first (live-fix.py log).')
        rule_a(history.rounds, now)
        return {'ok': True, 'phase': 'R'}
    rounds = history.counted(review=False)
    verdict = history.verdict()
    if verdict and verdict.get('verdict') in ('stop', 'passed'):
        raise Refused('stopped', f'The pass ended: {verdict["reason"]}', 'Deliver the best round (html-finish.py best), with its ledger.')
    if history.started and time.time() - history.started > BACKSTOP:
        raise Refused('backstop', 'The finishing pass has run for 90 minutes', 'Stop and deliver the best round with its ledger.')
    if len(rounds) >= POOL:
        raise Refused('pool', f'All {POOL} gate runs of the pass are used', 'Deliver the best round with its ledger.')
    f1 = [r for r in rounds if r['phase'] == 'F1']
    manifest_rounds = [r for r in f1 if rebuilt(history.rounds, r)]
    if args.phase == 'F1' and any(r['phase'] == 'F2' for r in rounds):
        raise Refused('order', 'The theme phase has started; a manifest change now discards its fixes', 'Stay in F2, or have the owner discard the live fixes first.')
    if args.phase == 'F1' and len(manifest_rounds) >= F1_MAX:
        raise Refused('f1-cap', f'F1 has had its {F1_MAX} manifest rounds', 'Deliver the best round with its ledger.')
    if args.phase == 'F2' and not (f1 and f1_done(history.rounds, f1[-1])):
        if f1 and not f1[-1]['structural']:
            raise Refused('f1-open', 'F2 starts after the manifest phase ran: the last F1 round measured HTML Flash\'s own manifest',
                          'Edit the manifest and rebuild, or state that the site read found nothing to declare '
                          '(round --phase F1 --nothing-to-declare).')
        raise Refused('f1-open', 'F2 starts when the last F1 round has no structural row failing', 'Finish the manifest phase first.')
    rule_a(history.rounds, now)
    return {'ok': True, 'phase': args.phase, 'runsLeft': POOL - len(rounds), 'f1RunsLeft': F1_MAX - len(manifest_rounds)}


def page_route(name):
    """verify-wp keys pages by their dist file: index.html → /, about.html → /about/."""
    stem = name[:-5] if name.endswith('.html') else name
    stem = stem[:-6] if stem.endswith('/index') else ('' if stem == 'index' else stem)
    return '/' + (stem.strip('/') + '/' if stem.strip('/') else '')


def rows_of(verify, smoke, woo):
    """(how many rows the gates measured, [the failing rows]). Structural: the
    wiring a manifest decides (routes, sources, blog, collections, menus and
    the editor's menu steps, shop, Woo); theme: the editor's other steps,
    which theme files decide; visual: the pixel and layout rows. F1 ends when
    no structural row fails; F2 takes the theme and visual rows. The pass or
    fail of a row is the gate's own, at its own thresholds."""
    measured, failing = 0, []

    def fail(id_, check, kind, route, what, width=None, diff=None):
        failing.append({'id': id_, 'check': check, 'kind': kind, 'route': route, 'width': width, 'what': what, 'diff': diff,
                        'cause': None, 'lever': None})
    for page, entry in (verify.get('pages') or {}).items():
        for name, value in (entry or {}).items():
            if isinstance(value, dict) and 'ok' in value:
                measured += 1
                if value['ok'] is False:
                    visual = 'diffRatio' in value
                    fail(f'verify-wp|{page_route(page)}|{name}', 'verify-wp', 'visual' if visual else 'structural', page_route(page),
                         f'{value["diffRatio"]:.2%} of the page differs' if visual else str(value.get('status') or 'failed'),
                         width=name, diff=value.get('diffRatio'))
        if (entry or {}).get('unresolvedTokens'):
            fail(f'verify-wp|{page_route(page)}|tokens', 'verify-wp', 'structural', page_route(page), 'an unresolved token prints as text')
        if (entry or {}).get('failedRequests'):
            fail(f'verify-wp|{page_route(page)}|requests', 'verify-wp', 'structural', page_route(page),
                 'requests WordPress fails: ' + ', '.join(entry['failedRequests'][:3]))
    for name, value in (verify.get('checks') or {}).items():
        if not isinstance(value, dict):
            continue
        measured += 1
        if value.get('ok') is False:
            fail(f'verify-wp|*|{name}', 'verify-wp', 'structural', '*', str(value.get('detail') or value.get('status') or 'failed')[:200])
        elif value.get('blockGapSeam') or value.get('collapsedGrids'):
            fail(f'verify-wp|{page_route(name)}|layout', 'verify-wp', 'visual', page_route(name), 'a layout seam or a collapsed grid')
    for step, value in (smoke.get('steps') or {}).items():
        if isinstance(value, dict) and 'ok' in value:
            measured += 1
            if value['ok'] is False:
                fail(f'editor|*|{step}', 'editor', 'structural' if step in EDITOR_WIRING else 'theme', '*',
                     str(value.get('detail') or value.get('extra') or 'the editor step failed')[:200])
    measured += len(woo.get('checked') or []) + len(woo.get('failures') or [])
    for key in woo.get('failures') or []:
        fail(f'woo|*|{key}', 'woo', 'structural', '*', f'Woo coverage: {key}')
    return measured, failing


def touched(ws, lay):
    """The theme files the open live-fix cycle changed from its base."""
    marker = read(Path(ws) / LIVE.MARKER) or read(Path(ws) / LIVE.STORE / LIVE.MARKER)
    if not marker or not marker.get('cycles') or marker['cycles'][-1].get('delivered'):
        return []
    changed = LIVE.changes(marker['cycles'][-1]['base']['files'], LIVE.file_hashes(lay['theme']))
    return changed['added'] + changed['modified'] + changed['removed']


def coverage_rows(files, smoke):
    """A failing row for every residue file the fix touched that the editor
    smoke did not exercise: the owner edits these in Visual Edit, so a round
    that did not open them in it has not passed."""
    steps = smoke.get('steps') or {}
    # A step that skipped (ok with `skipped`) exercised nothing.
    ok = lambda name: isinstance(steps.get(name), dict) and steps[name].get('ok') is True and not steps[name].get('skipped')
    listed = lambda step, field, key: any(isinstance(r, dict) and r.get('key') == key and r.get('ok') is True
                                          for r in (steps.get(step) or {}).get(field) or [])
    rows = []
    for rel in files:
        name = Path(rel).stem
        if rel.startswith('parts/article') or rel == 'clara-content/posts.json':
            need, what = ok('articlePart'), 'articlePart'
        elif rel.startswith('parts/'):
            need, what = listed('chromeParts', 'parts', name), f'chromeParts {name}'
        elif rel.startswith('patterns/') or rel == 'clara-content/sources/front-page.html':
            need, what = ok('textEditIdempotentSave'), 'textEditIdempotentSave'
        elif rel.startswith('clara-content/sources/'):
            need, what = listed('pageEditRoots', 'pages', name), f'pageEditRoots {name}'
        elif rel.startswith('templates/'):
            need, what = ok('pageEditRoots'), 'pageEditRoots'
        else:
            continue  # styles, scripts, media: the visual gates' business, not the editor's
        if not need:
            rows.append({'id': f'editor|*|coverage:{rel}', 'check': 'editor', 'kind': 'theme', 'route': '*', 'width': None,
                         'what': f'the editor smoke did not exercise {rel} (needs {what})', 'diff': None, 'cause': None, 'lever': None})
    return rows


def live_fixes(ws):
    marker = read(Path(ws) / LIVE.MARKER) or read(Path(ws) / LIVE.STORE / LIVE.MARKER)
    return len(marker['cycles'][-1]['fixes']) if marker and marker.get('cycles') and not marker['cycles'][-1].get('delivered') else None


def round_(args):
    """Record one gate run and say what comes next."""
    ws = Path(args.workspace).resolve()
    history = History(ws, args.history)
    now, lay, manifest = digests(ws)
    if getattr(args, 'nothing_to_declare', False):
        return nothing_to_declare(args, history, now, ws)
    shop = bool((manifest.get('shop') or {}).get('present'))
    paths = {'verify': Path(args.verify) if args.verify else ws / 'verify-wp/report.json',
             'smoke': Path(args.smoke) if args.smoke else ws / 'smoke-editor/report.json',
             'woo': Path(args.woo) if args.woo else ws / 'woo-coverage/report.json'}
    newest = LIVE.newest(lay['theme'])
    reports = {name: read(path) if path.is_file() and path.stat().st_mtime >= newest else None for name, path in paths.items()}
    # A run that died before it produced rows (a missing or stale report)
    # measured nothing, and does not count against the pool.
    died = [name for name in ('verify', 'smoke') if not reports[name]] + (['woo'] if shop and not reports['woo'] else [])
    measured, failing = rows_of(reports['verify'] or {}, reports['smoke'] or {}, reports['woo'] or {}) if not died else (0, [])
    if not died and args.phase in ('F2', 'R'):
        failing += coverage_rows(touched(ws, lay), reports['smoke'] or {})
    if args.phase == 'F1':
        write(ws / SNAPSHOTS / f'{now["manifestDigest"]}.json', manifest)
    # The reports this round read, kept, so the round delivered is the round
    # reported (end --restore puts them back).
    kept = ws / REPORTS / str(len(history.rounds) + 1)
    for name, path in paths.items():
        if reports[name] is not None:
            write(kept / f'{name}.json', reports[name])
    entry = {'n': len(history.rounds) + 1, 'phase': args.phase, 'at': stamp(), **now, 'rows': measured,
             'counted': not died and measured > 0, 'passed': not died and measured > 0 and not failing,
             'failing': failing, 'structural': [row['id'] for row in failing if row['kind'] == 'structural'], 'liveFixes': live_fixes(ws)}
    if died:
        entry['died'] = died
    verdict = judge(history, entry)
    history.append(entry, verdict)
    chosen = history.best() if not history.host else history.best(entry)
    index = next((i for i, r in enumerate(history.rounds + ([] if not history.host else [entry])) if r is chosen), None)
    return {'ok': True, 'round': entry, 'counted': entry['counted'], 'failing': len(entry['failing']), 'structural': len(entry['structural']),
            **verdict, 'best': index, 'bestRound': restore_of(chosen)}


def nothing_to_declare(args, history, now, ws):
    """The agent's statement that the site read found nothing the manifest
    must declare (no blog, shop, collection or menu left to wire): F1 ends
    on the round that measured HTML Flash's build, with no new gate run. An
    uncounted entry, appended by the host like a round."""
    if args.phase != 'F1':
        raise Refused('declare', 'Only the manifest phase (F1) takes --nothing-to-declare', 'Run the gates and record the round.')
    counted = history.counted(review=False)
    last = counted[-1] if counted else None
    if not last or last['phase'] != 'F1':
        raise Refused('declare', 'No F1 round to end: the statement follows the round measured on the Flash build',
                      'Run the gates on the Flash build and record the round first.')
    if last['state'] != now['state']:
        raise Refused('declare', f'The workspace changed after round {last["n"]}: that is a manifest round, not nothing to declare',
                      'Rebuild and run the gates (round --phase F1).')
    if last['structural']:
        raise Refused('declare', f'Round {last["n"]} has {len(last["structural"])} structural row(s) failing; the manifest phase must fix them',
                      'Edit the manifest and rebuild.')
    entry = {'n': len(history.rounds) + 1, 'phase': 'F1', 'at': stamp(), **now, 'rows': 0, 'counted': False, 'passed': False,
             'declared': 'nothing', 'of': last['n'], 'failing': [], 'structural': [], 'liveFixes': live_fixes(ws)}
    verdict = judge(history, last, rounds=history.rounds + [entry], earlier=[r for r in counted if r is not last])
    history.append(entry, verdict)
    chosen = history.best()
    index = next((i for i, r in enumerate(history.rounds) if r is chosen), None)
    return {'ok': True, 'round': entry, 'counted': False, 'failing': len(last['failing']), 'structural': 0,
            **verdict, 'best': index, 'bestRound': restore_of(chosen)}


def judge(history, entry, rounds=None, earlier=None):
    """What the bound says after a round: passed, continue, theme (F1 → F2),
    stop (rule B, the pool, the F1 cap), or ledger (a red review round)."""
    earlier = history.counted(review=False) if earlier is None else earlier
    rounds = (history.rounds + [entry]) if rounds is None else rounds
    left = POOL - len(earlier) - (1 if entry['counted'] and entry['phase'] != 'R' else 0)
    done = f1_done(rounds, entry) if entry['phase'] == 'F1' else True
    open_f1 = ('the manifest phase has not run: this round measured HTML Flash\'s own manifest. Declare what the site '
               'read found (blog, shop, collections, menus) and rebuild, or state there is nothing to declare '
               '(round --phase F1 --nothing-to-declare)')

    def say(verdict, message, next_=None, rule=None):
        return {'verdict': verdict, 'rule': rule, 'reason': message, 'message': message, 'next': next_, 'runsLeft': left}
    if not entry['counted']:
        return say('continue', 'the gates died before they produced rows; the run does not count', entry['phase'])
    if not entry['failing'] and done:
        return say('passed', 'every gate row passes')
    if entry['phase'] == 'R':
        return say('ledger', f'{len(entry["failing"])} row(s) stay red: write their ledger entries')
    # Rule B within one manifest: a rebuild measures a new site, and its
    # first round is the new baseline (a check that exists only once the
    # wiring does, like articlePart once there are articles, is not growth).
    same = [len(r['failing']) for r in earlier if r.get('manifestDigest') == entry.get('manifestDigest')]
    best = min(same, default=None)
    if best is not None and len(entry['failing']) >= best:
        return say('stop', f'round {entry["n"]} did not reduce the failing rows below {best} of the same manifest', rule='B')
    if left <= 0:
        return say('stop', f'the {POOL} gate runs are used')
    if entry['phase'] == 'F1':
        if done:
            return say('theme', 'the structure is green; the theme phase takes the visual and theme rows', 'F2')
        if not entry['structural']:
            return say('continue', open_f1, 'F1')
        manifest_rounds = [r for r in earlier if r['phase'] == 'F1' and rebuilt(rounds, r)] + ([entry] if rebuilt(rounds, entry) else [])
        if len(manifest_rounds) >= F1_MAX:
            return say('stop', f'the structure is not green after {F1_MAX} manifest rounds')
        return say('continue', f'{len(entry["structural"])} structural row(s) left for a correcting manifest round', 'F1')
    return say('continue', f'{len(entry["failing"])} row(s) left', 'F2')


def restore_of(chosen):
    if not chosen:
        return None
    how = (f'live-fix.py revert --to {chosen["liveFixes"]}' if chosen['phase'] in ('F2', 'R') and chosen.get('liveFixes') is not None
           else 'the round\'s manifest (html-finish.py restore), then build, screenshot and install')
    return {'round': chosen['n'], 'phase': chosen['phase'], 'failing': len(chosen['failing']), 'themeDigest': chosen['themeDigest'], 'restore': how}


def find_round(history, number):
    found = next((r for r in history.rounds if r['n'] == number and r.get('counted')), None)
    if not found:
        raise Refused('no-round', f'No counted round {number}', 'Name a counted round from html-finish.py status.')
    return found


def restore(args):
    """Put the workspace back to a round's state: an F2 round by live-fix
    revert, an F1 round by its manifest (the caller then rebuilds)."""
    ws = Path(args.workspace).resolve()
    history = History(ws, args.history)
    chosen = find_round(history, args.round)
    marker = read(ws / LIVE.MARKER) or read(ws / LIVE.STORE / LIVE.MARKER)
    cycle = marker['cycles'][-1] if marker and marker.get('cycles') and not marker['cycles'][-1].get('delivered') else None
    if chosen['phase'] == 'F1' and cycle and cycle['base']['themeDigest'] == chosen['themeDigest']:
        # The F1 build is the theme phase's base: back to it by reverting
        # every fix, no rebuild (one would be refused with fixes present).
        chosen = {**chosen, 'liveFixes': 0}
    if chosen.get('liveFixes') is not None and (chosen['phase'] in ('F2', 'R') or cycle):
        now, _, _ = digests(ws)
        if now['themeDigest'] == chosen['themeDigest']:
            return {'ok': True, 'round': chosen['n'], 'themeDigest': now['themeDigest'], 'matches': True, 'next': 'install'}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = LIVE.main(['revert', '--workspace', str(ws), '--to', str(chosen['liveFixes'])])
        result = json.loads(out.getvalue())
        if code:
            raise Refused(result['refusals'][0]['code'], result['refusals'][0]['message'], result['refusals'][0]['lever'])
        now, _, _ = digests(ws)
        return {'ok': True, 'round': chosen['n'], 'themeDigest': now['themeDigest'], 'matches': now['themeDigest'] == chosen['themeDigest'],
                'next': 'install'}
    snapshot = read(ws / SNAPSHOTS / f'{chosen["manifestDigest"]}.json')
    if snapshot is None:
        raise Refused('restore', f'Round {chosen["n"]}\'s manifest is not stored', 'Restore the manifest by hand, then rebuild.')
    write(ws / 'conversion-manifest.json', snapshot)
    return {'ok': True, 'round': chosen['n'], 'themeDigest': None, 'next': 'build, screenshot and install (the theme is rebuilt from this manifest)'}


def end(args):
    """The pass is over: with --restore, round N (default: the best) comes
    back, its theme or its manifest and the gate reports it was measured
    by, so the round delivered is the round reported (send-verdicts.sh reads
    those reports). Packaging then closes the live-fix cycle (live-fix.py
    verify-package / deliver, with --history and, when red, --ledger)."""
    ws = Path(args.workspace).resolve()
    history = History(ws, args.history)
    chosen = find_round(history, args.round) if args.round else history.best()
    if not chosen:
        raise Refused('no-round', 'No counted round yet', 'Run the gates and record a round first.')
    result = {'ok': True, 'round': chosen['n'], 'restored': False, **restore_of(chosen)}
    if args.restore:
        args.round = chosen['n']
        result.update(restore(args), restored=True)
        back = []
        for name, home in REPORT_HOMES.items():
            kept = ws / REPORTS / str(chosen['n']) / f'{name}.json'
            if kept.is_file():
                (ws / home).parent.mkdir(parents=True, exist_ok=True)
                (ws / home).write_bytes(kept.read_bytes())
                back.append(home)
        result['reports'] = back
    return result


def best(args):
    history = History(Path(args.workspace).resolve(), args.history)
    chosen = history.best()
    if not chosen:
        raise Refused('no-round', 'No counted round yet', 'Run the gates and record a round first.')
    return {'ok': True, **restore_of(chosen)}


WOO_PAGES = (('cart', '/cart/', 'Cart'), ('checkout', '/checkout/', 'Checkout'), ('account', '/my-account/', 'My account'))


def woo_defaults(manifest):
    """The shop pages the source did not have: WooCommerce's own, drawn by the
    theme's Woo defaults under the site's CSS. Named, never fabricated."""
    if not (manifest.get('shop') or {}).get('present'):
        return []
    kinds = {str(page.get('kind', '')) for page in manifest.get('pages') or [] if isinstance(page, dict)}
    return [{'page': kind, 'route': route, 'what': f"WooCommerce's own {title} page, drawn by the theme's Woo defaults under the site's CSS; the source had none"}
            for kind, route, title in WOO_PAGES if kind not in kinds and not (kind == 'account' and 'my-account' in kinds)]


def static_navs(ws):
    """The menus the generator could not wire (theme-report menusUnwired):
    each keeps the source's static navigation, named in the ledger, never an
    empty or invented menu."""
    report = read(Path(ws) / 'theme-report.json', {})
    base = 'menu not editable, static nav kept'
    return [{'location': item.get('location'), 'label': item.get('label'), 'region': item.get('region'),
             'what': base + (f' ({item["reason"]})' if item.get('reason') and item['reason'] != base else '')}
            for item in report.get('menusUnwired') or [] if isinstance(item, dict)]


def owner_rows(history, chosen):
    """The failing rows of `chosen` an owner's live fix turned red or made
    worse: in a review round up to it, failing and not failing (or failing by
    less) in the round that fix started from, the review round before it or
    else the round the pass delivered. Only these may be kept as the owner's
    choice."""
    number = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool)
    made, passes, prior = set(), [], None
    for r in (r for r in history.rounds if r.get('counted') and r['n'] <= chosen['n']):
        if r['phase'] != 'R':
            passes, prior = passes + [r], None
            continue
        base = prior or best_of(passes)
        if base:
            # With no round before the fix (a Flash delivery's first check),
            # nothing is measured as made worse: every row keeps its own cause.
            before = {row['id']: row for row in base.get('failing') or []}
            made |= {row['id'] for row in r['failing'] if row['id'] not in before
                     or number(row.get('diff')) and number(before[row['id']].get('diff')) and row['diff'] > before[row['id']]['diff']}
        prior = r
    return made & {row['id'] for row in chosen['failing']} if chosen['phase'] == 'R' else set()


def ledger(args):
    """Annotate a failing row of the delivered round, or write the ledger
    once every one of them says its cause and its lever."""
    ws = Path(args.workspace).resolve()
    history = History(ws, args.history)
    chosen = history.best()
    if not chosen:
        raise Refused('no-round', 'No counted round yet', 'Run the gates and record a round first.')
    rows = {row['id']: row for row in chosen['failing']}
    notes = read(ws / NOTES, {})
    if args.row:
        if args.row not in rows:
            raise Refused('row', f'{args.row} is not a failing row of the delivered round ({chosen["n"]})', 'Name one of the rows html-finish.py status lists.')
        if args.cause not in CAUSES or args.lever not in LEVERS or len((args.note or '').strip()) < 12:
            raise Refused('note', 'A ledger entry names its cause, its lever and what differs (12+ characters)',
                          f'--cause {"|".join(CAUSES)} --lever {"|".join(LEVERS)} --note "<what differs, where>"')
        if args.cause == 'owner-choice' and args.lever != 'report':
            raise Refused('owner-choice', 'A row the owner keeps is reported as it is: owner-choice takes --lever report',
                          '--cause owner-choice --lever report --note "Kept by the owner: <their reason>"')
        notes[args.row] = {'cause': args.cause, 'lever': args.lever, 'note': args.note.strip()}
    # The owner's choice is a row their live fix made; the host's rounds say
    # which, so a row the pass left red never carries it.
    made = owner_rows(history, chosen)
    claimed = [row for row in rows if (notes.get(row) or {}).get('cause') == 'owner-choice' and row not in made]
    if claimed:
        raise Refused('owner-choice', f'{claimed[0]} is not a row an owner\'s live fix turned red or made worse',
                      'owner-choice records the owner keeping such a row; any other row gets its converter-gap or source-ambiguity cause.')
    if args.row:
        write(ws / NOTES, notes)
    missing = [row for row in rows if row not in notes]
    if args.row:
        return {'ok': True, 'row': args.row, 'missing': missing}
    if missing:
        raise Refused('ledger', f'{len(missing)} failing row(s) of round {chosen["n"]} have no entry: ' + ', '.join(missing[:8]),
                      'html-finish.py ledger --row <id> --cause … --lever … --note "…" for each.')
    out = {'schema': 'h2wp-html-finish-ledger/1', 'round': chosen['n'], 'phase': chosen['phase'], 'themeDigest': chosen['themeDigest'],
           'status': 'green' if not rows else 'ledger', 'entries': [{**rows[row], **notes[row]} for row in rows],
           'wooDefaults': woo_defaults(read(ws / 'conversion-manifest.json') or {}), 'staticNav': static_navs(ws)}
    # The host writes it where the agent cannot (--out); packaging reads that copy.
    write(Path(args.out) if args.out else ws / LEDGER, out)
    return {'ok': True, **out}


def preflight_rows(ws):
    """The rows convert's pre-flight refused the manifest with
    (preflight-listings.json), while they describe the manifest as it is: a
    selector the service would leave unwired, fixed in the manifest before a
    rebuild can run. Empty once the manifest changed or passed."""
    value = read(Path(ws) / 'preflight-listings.json', {})
    manifest = Path(ws) / 'conversion-manifest.json'
    if not (isinstance(value, dict) and manifest.is_file()
            and value.get('manifestSha256') == hashlib.sha256(manifest.read_bytes()).hexdigest()):
        return []
    return [row for row in value.get('rows') or [] if isinstance(row, dict)]


def status(args):
    history = History(Path(args.workspace).resolve(), args.history)
    return {'ok': True, 'runsUsed': len(history.counted(review=False)), 'pool': POOL, 'rounds': history.rounds,
            'verdict': history.verdict(), 'best': restore_of(history.best()), 'notes': read(Path(args.workspace) / NOTES, {}),
            'preflight': preflight_rows(args.workspace)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('may-run', 'rerun', 'round', 'restore', 'end', 'best', 'ledger', 'status'):
        command = sub.add_parser(name)
        command.add_argument('--workspace', required=True)
        command.add_argument('--history', help="the host's round history (read only here); default: the workspace's")
        if name in ('may-run', 'rerun', 'round'):
            command.add_argument('--phase', choices=('F1', 'F2', 'R'), required=name == 'round')
        if name == 'round':
            command.add_argument('--nothing-to-declare', action='store_true', dest='nothing_to_declare',
                                 help="F1: the site read found nothing the manifest must declare; ends F1 on the Flash build's "
                                      'round without a gate run (an uncounted entry the host appends)')
            command.add_argument('--verify')
            command.add_argument('--smoke')
            command.add_argument('--woo')
        if name == 'restore':
            command.add_argument('--round', type=int, required=True)
        if name == 'end':
            command.add_argument('--round', type=int, help='the round to deliver (default: the best)')
            command.add_argument('--restore', action='store_true', help='put that round back: its theme or manifest, and its gate reports')
        if name == 'ledger':
            command.add_argument('--out', help="where the ledger goes (the host's folder); default: the workspace")
            command.add_argument('--row')
            command.add_argument('--cause')
            command.add_argument('--lever')
            command.add_argument('--note')
    args = parser.parse_args(argv)
    if args.command == 'rerun':
        args.command, args.phase = 'may-run', args.phase or 'F2'
    try:
        result = {'may-run': may_run, 'round': round_, 'restore': restore, 'end': end, 'best': best, 'ledger': ledger, 'status': status}[args.command](args)
    except (Refused, LIVE.Refused) as refused:
        result = {'ok': False, 'refusals': [refused.row]}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
