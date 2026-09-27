"""Astro artifact and route coverage checks, independent of the reduced manifest."""
import json
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from full_delivery import read

POLICY = 'astro-delivery/1'


def route_file(route):
    route = urlsplit(route).path.strip('/')
    return 'index.html' if not route else route if route.endswith('.html') else route + '.html'


def built_file(dist, file):
    return next((p for p in (dist / file, dist / (file[:-5] + '/index.html')) if p.is_file()), None)


def coverage(ws):
    ws = Path(ws)
    manifest = read(ws / 'conversion-manifest.json') or {}
    prerender = read(ws / 'prerender-report.json') or {}
    # The earlier inventory survives a partial recapture or a reduced manifest.
    inventory = read(ws / 'route-inventory.json') or {}
    routes = list(dict.fromkeys(inventory.get('routes', []) + prerender.get('routes', [])))
    expected = list(dict.fromkeys([route_file(r) for r in routes] +
                                 [p['file'] for p in manifest.get('pages', []) if p.get('file')]))
    dist = ws / 'astro-project/dist'
    missing = [f for f in expected if not built_file(dist, f)]
    errors = {f: p.get('captureError') for f, p in prerender.get('pages', {}).items() if p.get('captureError')}
    source_errors = {f: p.get('sourceErrors') for f, p in prerender.get('pages', {}).items() if p.get('sourceErrors')}
    failed = bool(prerender.get('failure') or errors or (prerender.get('coverage') or {}).get('passed') is False)
    return {'schema': 'h2wp-astro-coverage/1', 'expected': expected, 'missing': missing,
            'captureErrors': errors, 'sourceErrors': source_errors,
            'passed': bool(expected) and not missing and not failed and not source_errors,
            'detail': f'{len(expected)-len(missing)}/{len(expected)} expected pages built' +
                      ('; missing: ' + ', '.join(missing) if missing else '') +
                      ('; source runtime errors remain' if source_errors else '') +
                      ('; prerender failed or incomplete' if failed else '')}


def valid_zip(path):
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if not names or z.testzip():
                return False
            if any(n.startswith('/') or '..' in Path(n).parts for n in names):
                return False
            roots = {n.split('/')[0] for n in names}
            if len(roots) != 1:
                return False
            root = next(iter(roots))
            package = json.loads(z.read(root + '/package.json'))
            return bool(isinstance(package, dict) and isinstance(package.get('dependencies'), dict) and
                        package['dependencies'].get('astro') and
                        any(n.startswith(root + '/dist/') and n.endswith('.html') and z.getinfo(n).file_size for n in names))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return False
