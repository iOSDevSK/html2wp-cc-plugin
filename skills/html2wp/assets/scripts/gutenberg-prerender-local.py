#!/usr/bin/env python3
"""Capture a locally running SSR build with the existing SPA recorder/gates.

The application has already been built and started by the caller. --dist is
its public assets directory, not an SPA shell. No application server is
started or stopped here. Every recording and parity gate remains the same.
"""
import argparse
import importlib.util
from pathlib import Path
import sys
from urllib.parse import urlparse


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--source-url', required=True)
    options, rest = parser.parse_known_args()
    url = urlparse(options.source_url)
    if (url.scheme != 'http' or url.hostname not in ('localhost', '127.0.0.1', '::1')
            or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment):
        parser.error('--source-url must be an HTTP loopback origin, without credentials or a path')
    script = Path(__file__).with_name('prerender-spa.py')
    sys.argv = [str(script), *rest, '--skip-build']
    spec = importlib.util.spec_from_file_location('h2wp_local_capture', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not module.DIST.is_dir():
        parser.error('--dist must be the SSR build public assets directory')
    original_serve = module.serve

    class ExistingServer:
        def shutdown(self):
            pass

    def serve(directory, spa_fallback):
        if Path(directory).resolve() == module.DIST:
            return ExistingServer(), options.source_url.rstrip('/')
        return original_serve(directory, spa_fallback)

    module.serve = serve
    module.build = lambda: None  # explicitly prebuilt SSR; no index.html shell
    module.main()


if __name__ == '__main__':
    main()
