"""Is this URL safe for a browser we drive to fetch?

Extracted from mirror-live.py so it can be tested. It could not be, while it
lived in a script that parses argv at import time — and a security check
nobody can run is a security check nobody has verified.

THE THING THIS EXISTS FOR

Checking the URL a person typed is the easy half and it was the only half.
A mirror drives a real browser at a real page, and that page decides what
else to fetch: script tags, images, XHR, iframes, fonts. Every one of those
is a request the browser makes on the page's behalf, to an address the page
chose. So a site served from a perfectly ordinary public host can ask for

    <img src="http://169.254.169.254/latest/meta-data/iam/security-credentials/">
    fetch('http://192.168.1.1/admin/config')

and the mirror would fetch it, write it into the output directory, and ship
it onward into the conversion. The route handler that was supposed to catch
this looked only at the HTTP METHOD: a GET to the cloud metadata endpoint is
a GET, so it went through.

The check therefore belongs on every request, not on the entry point.

WHY RESOLUTION AND NOT A STRING BLOCKLIST

`localtest.me`, `127.0.0.1.nip.io` and a private A record on an ordinary
domain all read as public and all land somewhere private. Only the resolved
address answers the question. IPv4-mapped IPv6 (`::ffff:127.0.0.1`) is
unwrapped before judging, because otherwise it is a v6 address that looks
public and is not.

DNS is deliberately resolved afresh for every request, so a public answer is
not trusted for the lifetime of a crawl. There remains a narrow resolver-to-
connect race because Playwright cannot pin the checked address; eliminating
that last race requires a network proxy or namespace outside the browser.
"""

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

__all__ = [
    "address_verdict", "attach_network_guard", "guarded_api_get",
    "is_private_url", "reset_cache",
]


def _judge(ip):
    """True when this address is not somewhere the open web lives."""
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolved_private(host, port):
    """`"host -> ip"` when it lands somewhere private, else None.

    Resolved for every request. Reusing an earlier public answer creates an
    avoidable DNS-rebinding window: a hostname may later resolve privately.
    """
    # A literal needs no resolver, and must not depend on one. `http://
    # 169.254.169.254/` is the most direct form of this attack and also the
    # one where deferring to getaddrinfo is pointless — worse than pointless,
    # because any resolver hiccup then reads as "cannot tell, allow it".
    # Caught by the test suite, which faked resolution and let four bare-IP
    # cases through.
    literal = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ip = ipaddress.ip_address(literal)
    except ValueError:
        pass
    else:
        return f"{host} -> {ip}" if _judge(ip) else None

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        # Cannot resolve. Not our refusal to make — the fetch will fail on its
        # own, and reporting it as "private" would be a lie.
        return None
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _judge(ip):
            return f"{host} -> {ip.ipv4_mapped or ip if ip.version == 6 else ip}"
    return None


def reset_cache():
    """Compatibility no-op; resolutions are intentionally no longer cached."""
    return None


def is_private_url(url):
    """Reason to refuse this request, or None to allow it.

    Non-HTTP schemes return None: `data:`, `blob:` and `about:` fetch nothing
    over the network, so there is no address to judge and refusing them would
    break ordinary pages.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    host = parsed.hostname
    if not host:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return _resolved_private(host, port)


def address_verdict(url):
    """The stricter check, for the URL a person supplied rather than one a
    page asked for. Says why in a full sentence, and unlike is_private_url it
    objects to a URL it cannot resolve or cannot parse — a typo in `--site`
    should stop the run, while an unresolvable image on page four should not.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"{parsed.scheme or 'that'} is not a scheme this fetches"
    host = parsed.hostname
    if not host:
        return "there is no host in that URL"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as err:
        return f"{host} does not resolve ({err})"
    landed = _resolved_private(host, port)
    if landed:
        return f"{landed.split(' -> ')[1]} is not a public address ({host})"
    return None


def _origin(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname.lower().rstrip("."), port


def attach_network_guard(context, *, allowed_origins=(), checker=is_private_url,
                         allowed_methods=None, on_block=None):
    """Attach the private-address guard before a context makes any request.

    ``allowed_origins`` is exact (scheme, hostname and effective port), used
    for a local HTTP server owned by the caller. It never means "all
    loopback". ``checker`` lets a deliberate private-mirror override remain a
    single policy decision in mirror-live.py.
    """
    allowed = {_origin(value) for value in allowed_origins}
    allowed.discard(None)
    methods = {method.upper() for method in allowed_methods} if allowed_methods else None

    def guard(route):
        request = route.request
        reason = None if _origin(request.url) in allowed else checker(request.url)
        if reason:
            if on_block:
                on_block(request, reason)
            return route.abort()
        if methods is not None and request.method.upper() not in methods:
            if on_block:
                on_block(request, f"method {request.method} is not allowed")
            return route.abort()
        return route.continue_()

    context.route("**", guard)
    return guard


def guarded_api_get(api, url, *, checker=is_private_url, timeout=20000,
                    max_redirects=10, on_block=None):
    """GET while checking the initial URL and every redirect target."""
    current = url
    for _hop in range(max_redirects + 1):
        reason = checker(current)
        if reason:
            if on_block:
                on_block(current, reason)
            raise ValueError(f"private request refused: {current} ({reason})")
        response = api.get(current, timeout=timeout, max_redirects=0)
        if response.status not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("location")
        if not location:
            return response
        current = urljoin(current, location)
    raise ValueError(f"too many redirects while fetching {url}")
