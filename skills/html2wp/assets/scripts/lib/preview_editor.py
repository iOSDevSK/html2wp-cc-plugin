"""Activate the installed preview editor, preserving an active Pro installation."""
import json
import subprocess


def ensure(container, run=subprocess.run):
    base = ['docker', 'exec', container, 'wp', '--allow-root']
    def wp(*args):
        result = run(base + list(args), capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise RuntimeError('Visual Edit: WordPress command failed (' + ' '.join(args[:2]) + ')')
        return result.stdout
    def plugins():
        rows = json.loads(wp('plugin', 'list', '--format=json', '--fields=name,status'))
        return {r['name']: r['status'] for r in rows}
    found = plugins()
    if found.get('visual-edit') in ('active', 'active-network'):
        return {'status': 'active', 'plugin': 'visual-edit', 'changed': False}
    current = found.get('visual-edit-lite')
    if current is None:
        return {'status': 'not_installed', 'plugin': 'visual-edit-lite', 'changed': False}
    if current in ('active', 'active-network'):
        return {'status': 'active', 'plugin': 'visual-edit-lite', 'changed': False}
    wp('plugin', 'activate', 'visual-edit-lite')
    if plugins().get('visual-edit-lite') not in ('active', 'active-network'):
        raise RuntimeError('Visual Edit Lite did not become active')
    return {'status': 'active', 'plugin': 'visual-edit-lite', 'changed': True}
