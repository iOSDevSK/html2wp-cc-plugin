"""The local verification server must not hand out the caller's project.

verify-static.py serves a directory over loopback so root-relative paths
resolve, and that directory is somebody's working tree. The page it renders is
untrusted markup that issues its own requests, so `fetch('/.env')` used to be
a same-origin read of whatever sat beside the site.

Two halves are tested here: the policy on its own, and a REAL http.server
wired to it — because a policy that is correct and not actually installed in
the handler is the failure mode that looks fine in review.
"""

import functools
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from web_assets import refuse_request_path  # noqa: E402

failures = []


def check(label, condition):
    print(f"{'ok  ' if condition else 'FAIL'} — {label}")
    if not condition:
        failures.append(label)


class Guarded(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_head(self):
        if refuse_request_path(self.path, self.directory):
            self.send_error(404, "Not Found")
            return None
        return super().send_head()


def status_of(base, path):
    try:
        with urllib.request.urlopen(f"{base}{path}", timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


with TemporaryDirectory() as tmp:
    ROOT = Path(tmp)
    (ROOT / "index.html").write_text("<html><body>site</body></html>")
    (ROOT / "live.css").write_text("body{color:red}")
    (ROOT / "about").mkdir()
    (ROOT / "about" / "index.html").write_text("<html>about</html>")
    (ROOT / ".env").write_text("SECRET_KEY=hunter2")
    (ROOT / "backup.zip").write_text("not really a zip")
    (ROOT / "id_rsa").write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    (ROOT / ".git").mkdir()
    (ROOT / ".git" / "config").write_text("[remote origin]")

    print("== the policy ==")
    for target in [
        "/.env",
        "/.envrc",
        "/.git/config",
        "/.ssh/id_rsa",
        "/sub/.env",
        "/%2Eenv",                      # percent-encoded dot
        "/.aws/credentials",
    ]:
        check(f"refuses {target}", refuse_request_path(target, ROOT) is not None)

    for target in [
        "/config.yml",
        "/backup.zip",
        "/dump.sql",
        "/notes.md",
        "/database.db",
    ]:
        check(f"refuses non-web file {target}", refuse_request_path(target, ROOT) is not None)

    # Extensionless, and the distinction that needs the document root: a
    # directory is a route, a real file is somebody's private key.
    check("refuses an extensionless FILE (id_rsa)",
          refuse_request_path("/id_rsa", ROOT) is not None)
    check("serves an extensionless DIRECTORY route (/about)",
          refuse_request_path("/about", ROOT) is None)

    for target in [
        "/",
        "/index.html",
        "/live.css",
        "/assets/app.js",
        "/img/hero.webp",
        "/fonts/inter.woff2",
        "/blog/",
        "/style.css?v=3",               # query string ignored
        "/a%20b/logo.png",              # encoded space in a real name
    ]:
        check(f"serves {target}", refuse_request_path(target, ROOT) is None)

    check("refuses traversal", refuse_request_path("/../../etc/passwd", ROOT) is not None)

    print()
    print("== a real server wired to it ==")

    handler = functools.partial(Guarded, directory=str(ROOT))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    try:
        check("the site itself still loads", status_of(base, "/index.html") == 200)
        check("its stylesheet still loads", status_of(base, "/live.css") == 200)
        check("the directory index still loads", status_of(base, "/") == 200)
        check("an extensionless route still loads", status_of(base, "/about/") == 200)
        check("/.env is refused over real HTTP", status_of(base, "/.env") == 404)
        check("/.git/config is refused over real HTTP", status_of(base, "/.git/config") == 404)
        check("/backup.zip is refused over real HTTP", status_of(base, "/backup.zip") == 404)
        check("/id_rsa is refused over real HTTP", status_of(base, "/id_rsa") == 404)
        # The refusal must not distinguish a file that exists from one that
        # does not, or it answers the question it was meant to refuse.
        check(
            "a refused-but-present file is indistinguishable from a missing one",
            status_of(base, "/.env") == status_of(base, "/.nothing-here"),
        )
    finally:
        httpd.shutdown()

print()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("ALL OK")
