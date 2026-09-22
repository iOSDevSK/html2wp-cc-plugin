#!/usr/bin/env python3
"""Capture a genuine 1200x900 localhost frontend theme thumbnail, without image editing."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlparse
from urllib.request import urlopen
import hashlib
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', required=True, help='HTTP localhost WordPress origin')
    parser.add_argument('--theme-dir', required=True)
    parser.add_argument('--path', default='/', help='Frontend path, default homepage')
    parser.add_argument('--container', help='Optional isolated h2wp-* Docker WordPress container')
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('h2wp_package', Path(__file__).with_name('gutenberg-package.py'))
    package = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package)
    origin = urlparse(args.site)
    if not package.local_url(args.site) or origin.path not in ('', '/') or origin.query or origin.fragment:
        parser.error('--site must be an HTTP localhost origin without credentials or path')
    if (not args.path.startswith('/') or args.path.startswith('//') or '?' in args.path or '#' in args.path
            or any(part in args.path.split('/') for part in ('wp-admin', 'wp-login.php'))):
        parser.error('--path must identify a frontend path')
    theme = Path(args.theme_dir).resolve()
    if not (theme / 'style.css').is_file() or not (theme / 'functions.php').is_file():
        parser.error('--theme-dir must contain a generated WordPress theme')
    if args.container and not re.fullmatch(r'h2wp-[a-zA-Z0-9_.-]+', args.container):
        parser.error('--container must name an isolated h2wp-* Docker environment')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', theme.name):
        parser.error('Theme folder must be a valid lowercase WordPress theme slug')
    destination = theme / 'screenshot.png'
    with tempfile.NamedTemporaryFile(prefix='h2wp-preview-', suffix='.png', dir=theme.parent, delete=False) as temporary:
        staging = Path(temporary.name)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={'width': 1200, 'height': 900}, device_scale_factor=1,
                                    reduced_motion='reduce')
            response = page.goto(args.site.rstrip('/') + args.path, wait_until='networkidle')
            if (not response or response.status != 200 or not package.local_url(page.url)
                    or 'text/html' not in response.headers.get('content-type', '').lower()):
                raise RuntimeError('Frontend capture did not return a successful localhost page')
            if any(part in urlparse(page.url).path.split('/') for part in ('wp-admin', 'wp-login.php')):
                raise RuntimeError('Frontend capture redirected to WordPress administration')
            page.evaluate('''async () => {
              await document.fonts.ready;
              await Promise.all([...document.images].filter(image=>image.getBoundingClientRect().top<900)
                .map(image=>image.decode().catch(()=>{})));
            }''')
            page.wait_for_timeout(700)
            source_url = page.url
            final_url = urlparse(source_url)
            if (not package.local_url(source_url) or (final_url.port or 80) != (origin.port or 80)
                    or any(part in final_url.path.split('/') for part in ('wp-admin', 'wp-login.php'))):
                raise RuntimeError('Frontend navigated away from the captured localhost site')
            page.screenshot(path=str(staging), full_page=False, type='png')
            browser.close()
        info = package.screenshot_info(staging)
        # NamedTemporaryFile starts private (0600); Apache must read the preview.
        staging.chmod(0o644)
        staging.replace(destination)
        if args.container:
            ports = json.loads(subprocess.check_output(['docker', 'inspect', '--format',
                '{{json .NetworkSettings.Ports}}', args.container], text=True))
            bindings = ports.get('80/tcp') or []
            if not any(item.get('HostIp') in ('127.0.0.1', '::1')
                       and int(item.get('HostPort', 0)) == (origin.port or 80) for item in bindings):
                raise RuntimeError('Docker destination is not the localhost WordPress port that was captured')
            subprocess.run(['docker', 'cp', str(destination),
                            args.container + ':/var/www/html/wp-content/themes/' + theme.name + '/screenshot.png'], check=True)
            remote = args.site.rstrip('/') + '/wp-content/themes/' + theme.name + '/screenshot.png'
            with urlopen(remote, timeout=30) as response:
                if response.status != 200 or hashlib.sha256(response.read()).hexdigest() != info['sha256']:
                    raise RuntimeError('Installed thumbnail is not publicly readable with identical bytes')
        print(json.dumps({**info, 'sourceUrl': source_url, 'file': str(destination),
                          'installedToContainer': args.container or None}))
    finally:
        staging.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
