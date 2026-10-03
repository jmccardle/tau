"""What ``tau serve`` answers before a WebSocket handshake (docs/TAU-SERVE.md §6.2).

Two jobs, both in ``websockets``' ``process_request`` hook:

- **Origin check, on by default.** Browsers do not apply the same-origin policy
  to WebSocket handshakes, so without it any page the user has open could drive
  the agent. A request whose ``Origin`` is present and names neither its own
  ``Host`` nor an entry of ``serve.allowed_origins`` is refused with 403.
  Non-browser clients send no ``Origin`` and are unaffected; comparing against
  ``Host`` rather than the bind address keeps ``ssh -L`` and port remaps working.
- **The web client.** A plain HTTP request (no ``Upgrade: websocket``) is served
  from ``serve.web_root``, so the page and its socket share one origin.
"""

from __future__ import annotations

import mimetypes
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from websockets.datastructures import Headers
from websockets.http11 import Request, Response

ProcessRequest = Callable[[Any, Request], Response | None]


def origin_allowed(origin: str | None, host: str | None, allowed: frozenset[str]) -> bool:
    """Whether a handshake with this ``Origin`` and ``Host`` may proceed.

    No ``Origin`` (every non-browser client) is allowed. Otherwise the origin's
    ``host[:port]`` must equal ``Host``, or the whole origin must be in ``allowed``.
    """
    if origin is None:
        return True
    if origin in allowed:
        return True
    netloc = urlsplit(origin).netloc
    return bool(netloc) and host is not None and netloc.lower() == host.lower()


def _respond(status: int, reason: str, body: bytes, content_type: str) -> Response:
    headers = Headers()
    headers["Content-Type"] = content_type
    headers["Content-Length"] = str(len(body))
    headers["Connection"] = "close"
    return Response(status, reason, headers, body)


def _text(status: int, reason: str, text: str) -> Response:
    return _respond(status, reason, text.encode(), "text/plain; charset=utf-8")


def static_response(root: Path, request_path: str) -> Response:
    """The file under ``root`` that ``request_path`` names, ``index.html`` for a directory.

    A path that resolves outside ``root`` is a 404, the same as a missing file.
    """
    relative = unquote(urlsplit(request_path).path).lstrip("/")
    target = (root / relative).resolve()
    if target.is_dir():
        target = target / "index.html"
    if not target.is_relative_to(root) or not target.is_file():
        return _text(404, "Not Found", f"no such file: /{relative}\n")
    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return _respond(200, "OK", target.read_bytes(), content_type)


def build_process_request(
    serve_config: dict[str, Any], log: Callable[[str], None]
) -> ProcessRequest:
    """The ``process_request`` hook for ``serve.allowed_origins`` and ``serve.web_root``.

    Raises:
        ValueError: ``serve.web_root`` is set and is not a directory.
    """
    allowed = frozenset(str(o).rstrip("/") for o in serve_config.get("allowed_origins") or [])
    web_root_value = serve_config.get("web_root")
    web_root = Path(web_root_value).expanduser().resolve() if web_root_value else None
    if web_root is not None and not web_root.is_dir():
        raise ValueError(f"serve.web_root {str(web_root)!r} is not a directory")

    def process_request(connection: Any, request: Request) -> Response | None:
        headers = request.headers
        if "websocket" not in headers.get("Upgrade", "").lower():
            if web_root is None:
                return _text(
                    404,
                    "Not Found",
                    "this is a tau serve socket; set serve.web_root to host a page\n",
                )
            return static_response(web_root, request.path)
        origin = headers.get("Origin")
        if not origin_allowed(origin, headers.get("Host"), allowed):
            log(f"handshake refused: Origin {origin} is not Host {headers.get('Host')}")
            return _text(403, "Forbidden", "origin not allowed; see serve.allowed_origins\n")
        return None

    return process_request
