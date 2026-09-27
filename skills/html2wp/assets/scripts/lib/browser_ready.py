"""Network-idle readiness with media streams excluded, not data or scripts."""
import time
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


def track_page(page):
    if hasattr(page, '_h2wp_network'):
        return
    state = {'pending': set(), 'media': set(), 'last': time.monotonic(), 'reported': False}
    page._h2wp_network = state

    def started(request):
        if request.resource_type == 'media':
            state['media'].add(request)
        else:
            state['pending'].add(request)
            state['last'] = time.monotonic()

    def finished(request):
        state['media'].discard(request)
        if request in state['pending']:
            state['pending'].discard(request)
            state['last'] = time.monotonic()

    page.on('request', started)
    page.on('requestfinished', finished)
    page.on('requestfailed', finished)


def track_context(context):
    context.on('page', track_page)
    for page in context.pages:
        track_page(page)


def remaining(deadline):
    budget = int((deadline - time.monotonic()) * 1000)
    if budget <= 0:
        raise PlaywrightTimeoutError('Document and non-media requests exceeded the navigation budget')
    return budget


def wait_ready(page, warn=None, timeout=30000, _deadline=None):
    deadline = _deadline if _deadline is not None else time.monotonic() + timeout / 1000
    # Late callers without a complete request history keep native semantics.
    if not hasattr(page, '_h2wp_network'):
        page.wait_for_load_state('networkidle', timeout=remaining(deadline))
        return
    page.wait_for_load_state('domcontentloaded', timeout=remaining(deadline))
    state = page._h2wp_network
    quiet_since = time.monotonic()
    while time.monotonic() < deadline:
        now = time.monotonic()
        if state['pending']:
            quiet_since = now
        if not state['pending'] and now - max(quiet_since, state['last']) >= 0.5:
            if state['media'] and not state['reported']:
                if warn:
                    warn('network readiness: media stream still active; document and non-media requests settled')
                state['reported'] = True
            return
        page.wait_for_timeout(50)
    raise PlaywrightTimeoutError('Non-media requests did not settle within the navigation budget')


def goto_ready(page, url, warn=None, timeout=30000):
    deadline = time.monotonic() + timeout / 1000
    track_page(page)
    response = page.goto(url, wait_until='domcontentloaded', timeout=remaining(deadline))
    if response is not None and response.status >= 400:
        raise RuntimeError(f'Source document returned HTTP {response.status}')
    wait_ready(page, warn=warn, _deadline=deadline)
    return response
