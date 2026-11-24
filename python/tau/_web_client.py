"""The web client the HTTP edge serves (D29 section 9): static files, without a token.

Where the files come from, first found: `HttpEdge.web_root` (`tau serve --web-root DIR`), else the
`dist/` directory of an installed package `tau_web` (a web client built elsewhere, tau-code's, and
installed beside τ), else none, and `/` is the **help page**: what this URL is, why its
`#token=...` never reached the server, and how to get a client. A client's directory must hold
`index.html` (`ValueError` otherwise: a client that cannot load is said at start, not at the first
page). Nothing here reads the session: the files are the same for anyone who asks.

A path that names a file under the root is that file; one that escapes the root (`..`, an encoded
`..`, a backslash) is `404`; one with no extension is a page of the client's own routing and gets
`index.html`; any other is `404`. Every answer carries a strict Content-Security-Policy (scripts,
styles and connections to this origin alone), `nosniff` and `no-referrer`.
"""

import importlib.util
import mimetypes
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final
from urllib.parse import unquote

PACKAGE: Final = "tau_web"
"""The package a web client is installed as; its `dist/` directory is served."""
INDEX: Final = "index.html"

HEADERS: Final = (
    ("Cache-Control", "no-cache"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    (
        "Content-Security-Policy",
        "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; base-uri 'none'; "
        "frame-ancestors 'none'",  # rules: allow(frame): a CSP directive's name
    ),
)
"""What every page and file is sent with."""

HELP: Final = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>tau serve</title>
<style>
body { font: 16px/1.5 system-ui, sans-serif; max-width: 42rem; margin: 3rem auto; padding: 0 1rem; }
code { background: #8881; padding: 0 .25em; border-radius: 3px; }
</style>
</head>
<body>
<h1>τ: <code>tau serve</code></h1>
<p>This is the HTTP edge of a <code>tau serve</code> session. No web client is installed in this
τ, so there is nothing to open here yet.</p>
<p>The <code>#token=…</code> at the end of the URL you opened is for a web client: browsers never
send that part to a server, so the client reads it from the page's own address and sends it as a
bearer token. Nothing this page could do with it is needed.</p>
<h2>Get a client</h2>
<ul>
<li>Run <code>tau serve --web-root DIR</code> with a built web client in <code>DIR</code>
(its <code>index.html</code> and files).</li>
<li>Or install a client package that provides <code>tau_web</code> beside τ; <code>tau serve</code>
then serves it here.</li>
</ul>
<p>Programs speak the wire at <code>/stream</code>: see τ's <code>docs/guide/protocol.md</code>.</p>
</body>
</html>
"""


@dataclass(frozen=True)
class Page:
    """An answer: its `status`, `body` and `content_type`."""

    status: int
    body: bytes
    content_type: str


@dataclass(frozen=True)
class WebClient:
    """The files the edge serves: the client's `root`, or `None` for the help page alone."""

    root: Path | None

    def page(self, target: str) -> Page:
        """What a GET of `target` (a request target: a path and maybe a query) answers."""
        path = unquote(target.split("?", 1)[0])
        if self.root is None:
            if path in ("/", "/" + INDEX):
                return Page(200, HELP.encode(), "text/html; charset=utf-8")
            return _missing()
        found = _under(self.root, path)
        if found is None:
            return _missing()
        return _file(found)


def _missing() -> Page:
    return Page(404, b"not found\n", "text/plain; charset=utf-8")


def _under(root: Path, path: str) -> Path | None:
    """The file `path` names under `root`: itself, its directory's `index.html`, or (a path with
    no extension, the client's own route) the root's `index.html`. `None` for one that escapes
    `root` or names nothing."""
    parts = PurePosixPath(path).parts[1:]  # without the leading "/"
    if any(p == ".." or "\\" in p or "\0" in p for p in parts):
        return None
    candidate = root.joinpath(*parts).resolve()
    if not candidate.is_relative_to(root):
        return None  # a link out of the root
    if candidate.is_dir():
        candidate = candidate / INDEX
    if candidate.is_file():
        return candidate
    if parts and "." not in parts[-1]:
        return root / INDEX
    return None


def _file(path: Path) -> Page:
    kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if kind.startswith("text/") or kind in ("application/javascript", "application/json"):
        kind += "; charset=utf-8"
    return Page(200, path.read_bytes(), kind)


def checked_root(root: Path) -> Path:
    """`root`, resolved, if it holds a client (`index.html`); `ValueError` naming it otherwise."""
    resolved = root.resolve()
    if not (resolved / INDEX).is_file():
        raise ValueError(f"{root} holds no {INDEX}: it is not a built web client")
    return resolved


def installed() -> Path | None:
    """The `dist/` of an installed `tau_web`, or `None` if there is none. `ValueError` for a
    `tau_web` without a `dist/index.html`."""
    spec = importlib.util.find_spec(PACKAGE)
    if spec is None or not spec.submodule_search_locations:
        return None
    return checked_root(Path(next(iter(spec.submodule_search_locations))) / "dist")


def web_client(root: Path | None) -> WebClient:
    """The client to serve: `root` if given, else an installed `tau_web`'s, else none (the help
    page). `ValueError` for a root that holds no client."""
    return WebClient(checked_root(root) if root is not None else installed())
