"""What a local verification server is allowed to hand a browser.

Extracted from verify-static.py for the same reason net_guard.py was: it is a
security check, and a security check that cannot be run on its own is one
nobody has verified.

THE THING THIS EXISTS FOR

The gate serves a directory over loopback HTTP so that root-relative paths
(`/live.css`) resolve the way they will in production. The directory it serves
is the caller's own project, and the document it renders from there is
untrusted — it is the site being converted, which may be a Lovable export, a
client's zip, or something downloaded from an address in a ticket.

That page chooses its own requests. `fetch('/.env')` is a same-origin GET on a
server that was happily answering for every file under the project root, and
the reply lands inside a page that can then send it anywhere. Serving fewer
files is one half of closing that; the browser-side egress guard is the other.

WHY AN ALLOWLIST

A denylist has to name the secret. `.env` is easy, `.git/config` is easy, and
then it is `.envrc`, `.npmrc`, `.aws/credentials`, `id_rsa`, `backup.zip`,
`dump.sql`, `notes-with-the-password.md` — an open-ended list where being
wrong is silent. A static site needs a closed set of file types to render, so
the closed set is what gets served and everything else is a 404.
"""

from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

#: Suffixes a browser legitimately requests when rendering a static site.
WEB_SUFFIXES = frozenset({
    ".html", ".htm", ".css", ".js", ".mjs", ".cjs", ".json", ".map",
    ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico", ".bmp",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".txt", ".xml", ".webmanifest",
    ".mp4", ".webm", ".ogg", ".mp3", ".wav",
    ".pdf",
})


def refuse_request_path(request_path, root=None):
    """Should the server refuse this request target?

    Returns a reason string when it must be refused, or None when it may be
    served. Takes the raw request target (`/a/b.css?v=2`), because that is
    what the handler has and decoding it is part of the check.

    ``root`` is the document root. It is optional so the policy can be
    reasoned about on its own, but the handler must pass it: without it an
    extensionless target has to be given the benefit of the doubt (it is
    usually a directory, i.e. `/about` -> `/about/index.html`), and
    `id_rsa`, `Dockerfile` and `credentials` are extensionless too.
    """
    path = unquote(urlparse(request_path).path)
    parts = [p for p in path.split("/") if p not in ("", ".")]

    # Traversal. http.server already collapses this before it reaches a
    # handler, but a check that depends on someone else's normalisation is a
    # check that breaks quietly when that someone else changes.
    if any(p == ".." for p in parts):
        return "path traversal"

    # Anything hidden, at any depth: .env, .git/, .ssh/, .npmrc, .aws/.
    if any(p.startswith(".") for p in parts):
        return "hidden file or directory"

    if not parts:
        return None                       # "/" — the directory index

    suffix = PurePosixPath(parts[-1]).suffix.lower()
    if not suffix:
        # A directory is a route and is fine. A FILE with no extension is not
        # something a static site renders — it is `id_rsa`, `Dockerfile`,
        # `Procfile`, `credentials`.
        if root is None:
            return None
        target = _resolve(root, parts)
        if target is None:
            return "outside the document root"
        if target.is_dir() or not target.exists():
            return None
        return "extensionless file is not a web asset"

    if suffix not in WEB_SUFFIXES:
        return f"{suffix} is not a web asset type"
    return None


def _resolve(root, parts):
    """The requested path under root, or None if it escapes it."""
    from pathlib import Path

    base = Path(root).resolve()
    try:
        target = base.joinpath(*parts).resolve()
    except OSError:
        return None
    return target if target == base or base in target.parents else None
