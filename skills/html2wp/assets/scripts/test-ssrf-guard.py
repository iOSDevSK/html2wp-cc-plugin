"""The address predicate, exercised directly. See test-ssrf-guard.sh for why."""

import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import net_guard  # noqa: E402
from net_guard import address_verdict, is_private_url  # noqa: E402

REFUSE = "refuse"
ALLOW = "allow"

# Resolution is faked so the suite needs no network and cannot flake on DNS.
# The names are the shapes that matter, not the addresses they really have.
FAKE = {
    "metadata.example": "169.254.169.254",   # cloud metadata, the classic
    "router.example": "192.168.1.1",         # RFC1918
    "ten.example": "10.0.0.5",
    "carrier.example": "172.16.4.4",
    "loopback.example": "127.0.0.1",         # a PUBLIC name pointing home
    "mapped.example": "::ffff:127.0.0.1",    # v4 wrapped in v6
    "sixlocal.example": "fd00::1",           # unique-local v6
    "linklocal6.example": "fe80::1",
    "zero.example": "0.0.0.0",
    "public.example": "93.184.216.34",
    "alsopublic.example": "2606:2800:220:1::",
}
rebind_calls = 0


def fake_getaddrinfo(host, port, *a, **k):
    global rebind_calls
    if host == "rebind.example":
        rebind_calls += 1
        raw = "93.184.216.34" if rebind_calls == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (raw, port))]
    # A literal is never asked of the resolver by the code under test; if this
    # is ever reached with one, the literal shortcut has regressed.
    if host in FAKE:
        raw = FAKE[host]
        fam = socket.AF_INET6 if ":" in raw else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (raw, port))]
    raise OSError(f"no fake entry for {host}")


net_guard.socket.getaddrinfo = fake_getaddrinfo
net_guard.reset_cache()

cases = [
    # --- must be refused: the page asked for somewhere private -------------
    ("http://metadata.example/latest/meta-data/iam/", REFUSE),
    ("http://169.254.169.254/latest/meta-data/", REFUSE),
    ("http://router.example/admin", REFUSE),
    ("http://192.168.1.1/", REFUSE),
    ("http://ten.example/x", REFUSE),
    ("http://carrier.example/x", REFUSE),
    ("http://127.0.0.1:8080/secret", REFUSE),
    ("http://[::1]:9000/secret", REFUSE),
    # A perfectly ordinary hostname is not evidence of a public address —
    # this is the case a string blocklist cannot see.
    ("http://loopback.example/x", REFUSE),
    ("http://mapped.example/x", REFUSE),
    ("http://sixlocal.example/x", REFUSE),
    ("http://linklocal6.example/x", REFUSE),
    ("http://zero.example/x", REFUSE),
    ("https://router.example/over-tls", REFUSE),

    # --- must be allowed: ordinary web traffic ------------------------------
    ("https://public.example/app.js", ALLOW),
    ("http://public.example/img.png", ALLOW),
    ("https://alsopublic.example/font.woff2", ALLOW),
    # Nothing is fetched over the network for these, so there is no address to
    # judge and refusing them would break real pages.
    ("data:image/png;base64,iVBORw0KGgo=", ALLOW),
    ("blob:https://public.example/abc-123", ALLOW),
    ("about:blank", ALLOW),
    # Unresolvable is not the same as private. The fetch fails on its own.
    ("https://does-not-resolve.invalid/x", ALLOW),
]

failed = 0
for url, want in cases:
    got = REFUSE if is_private_url(url) else ALLOW
    if got != want:
        print(f"  FAIL {url}: wanted {want}, got {got}")
        failed += 1

# address_verdict is the stricter sibling used for --site, where a typo should
# stop the run rather than be waved through as "not private".
strict = [
    ("http://metadata.example/", True),
    ("https://public.example/", False),
    ("ftp://public.example/", True),          # not a scheme this fetches
    ("https://does-not-resolve.invalid/", True),  # --site must resolve
    ("http:///nohost", True),
]
for url, want_refusal in strict:
    got = address_verdict(url) is not None
    if got != want_refusal:
        print(f"  FAIL address_verdict({url}): wanted refusal={want_refusal}, got {got}")
        failed += 1

# A public answer must not be cached for the crawl. The next request sees the
# private answer and is refused.
if is_private_url("https://rebind.example/first") is not None:
    print("  FAIL rebinding setup: first public answer was refused")
    failed += 1
if is_private_url("https://rebind.example/second") is None:
    print("  FAIL DNS answers were cached; the private rebound address passed")
    failed += 1


class FakeRequest:
    def __init__(self, url, method="GET"):
        self.url, self.method = url, method


class FakeRoute:
    def __init__(self, url, method="GET"):
        self.request = FakeRequest(url, method)
        self.action = None

    def abort(self):
        self.action = "abort"

    def continue_(self):
        self.action = "continue"


class FakeContext:
    def route(self, pattern, handler):
        assert pattern == "**"
        self.handler = handler


ctx = FakeContext()
net_guard.attach_network_guard(
    ctx, allowed_origins=("http://127.0.0.1:8123",), allowed_methods=("GET",)
)
route_cases = [
    (FakeRoute("http://127.0.0.1:8123/index.html"), "continue"),
    (FakeRoute("http://127.0.0.1:9999/secret"), "abort"),
    (FakeRoute("http://metadata.example/latest/meta-data"), "abort"),
    (FakeRoute("https://public.example/write", "POST"), "abort"),
]
for route, wanted in route_cases:
    ctx.handler(route)
    if route.action != wanted:
        print(f"  FAIL reusable context guard: {route.request.url} wanted {wanted}, got {route.action}")
        failed += 1


class FakeResponse:
    def __init__(self, status, location=None):
        self.status = status
        self.headers = {} if location is None else {"location": location}


class FakeApi:
    def __init__(self):
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append((url, kwargs.get("max_redirects")))
        return FakeResponse(302, "http://127.0.0.1/private")


fake_api = FakeApi()
try:
    net_guard.guarded_api_get(fake_api, "https://public.example/asset")
except ValueError:
    pass
else:
    print("  FAIL API redirect guard followed a redirect to loopback")
    failed += 1
if fake_api.urls != [("https://public.example/asset", 0)]:
    print(f"  FAIL API guard did not disable automatic redirects: {fake_api.urls}")
    failed += 1

total = len(cases) + len(strict) + 2 + len(route_cases) + 2
if failed:
    print(f"{failed} of {total} cases failed")
    sys.exit(1)
print(f"  {total} cases pass")
