"""A local web server, and the built-in web extensions built as a start builds them (D24 section 9):
from config files and an environment, never by importing a class. No test here reaches the
network: every address is the server's own (127.0.0.1)."""

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tau import Extension, load_config, load_extensions


@dataclass
class Page:
    """What the site answers at a path."""

    body: bytes | str = b""
    type: str | None = "text/html; charset=utf-8"
    status: int = 200
    headers: dict[str, str] = field(default_factory=lambda: {})
    pause: float = 0  # seconds before it answers
    encoding: str | None = None


class Site:
    """A web server on 127.0.0.1 answering the `pages` it is given by path (404 for any other), and
    keeping the headers of each request it saw."""

    def __init__(self, pages: Mapping[str, Page]) -> None:
        self.pages = dict(pages)
        self.seen: list[tuple[str, dict[str, str]]] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = self.path
                site.seen.append((path, dict(self.headers.items())))
                page = site.pages.get(path.split("?")[0], Page("not found", "text/plain", 404))
                body = page.body.encode() if isinstance(page.body, str) else page.body
                if page.pause:
                    threading.Event().wait(page.pause)
                self.send_response(page.status)
                if page.type is not None:
                    self.send_header("Content-Type", page.type)
                if page.encoding is not None:
                    self.send_header("Content-Encoding", page.encoding)
                for name, value in page.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def other(self) -> str:
        """The same server under another host name: `localhost`."""
        return f"http://localhost:{self.server.server_address[1]}"


@contextmanager
def serving(pages: Mapping[str, Page]) -> Iterator[Site]:
    site = Site(pages)
    site.thread.start()
    try:
        yield site
    finally:
        site.server.shutdown()
        site.server.server_close()
        site.thread.join()


def built(
    tmp_path: Path,
    name: str,
    constants: Mapping[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Extension:
    """The built-in extension `name`, built as a start builds it: from a config whose
    `extensions.<name>` holds `constants`, and `environ` over an empty one. What the factory
    refuses raises here (`ConfigError`)."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    environment = {"HOME": str(home), "TAU_HOME": str(home), **(environ or {})}
    cli: dict[str, object] = {"load": {name: True}}
    if constants is not None:
        cli["extensions"] = {name: dict(constants)}
    loaded = load_config(
        home, environment, ask=None, cli=cli
    )  # the walk to a project stops at HOME
    (found,) = [e for e in load_extensions(loaded, discover=False) if e.name == name]
    return found
