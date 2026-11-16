"""`tau-a2a`: serve a τ session (one `tau serve` socket) over A2A 1.0, JSON-RPC binding."""

import argparse
import ipaddress
import logging
import sys
from pathlib import Path

import uvicorn

from tau_a2a._app import Settings, create_app, read_overlay
from tau_a2a._auth import JwtSettings


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tau-a2a", description=__doc__)
    p.add_argument("--socket", required=True, help="the `tau serve` socket of the session")
    p.add_argument("--db", required=True, type=Path, help="SQLite file for tasks and contexts")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--url", help="the URL callers use (the card's); default http://HOST:PORT/")
    p.add_argument("--name", default="tau")
    p.add_argument("--description")
    p.add_argument("--version", help="the card's version; default <session id>@<fork node>")
    p.add_argument(
        "--at",
        type=int,
        help="the node new contexts open at; default the end of "
        "the session's prefix (before its first user message)",
    )
    p.add_argument(
        "--card-overlay",
        type=Path,
        help="a JSON file, part of an A2A AgentCard (skills, provider, description, version, "
        "capabilities.extensions, ...), merged over the card the server states",
    )
    p.add_argument("--client-id", default="a2a", help="this server's client id on the session")
    p.add_argument(
        "--auth",
        choices=["none", "jwt"],
        required=True,
        help="none: no credential (loopback, or a port only a trusted proxy reaches); "
        "jwt: a bearer JWT checked against --issuer, --audience and its keys",
    )
    p.add_argument("--issuer", help="jwt: the expected `iss`; also where discovery is read")
    p.add_argument("--audience", help="jwt: the expected `aud`")
    p.add_argument("--jwks-url", help="jwt: the keys; default from the issuer's discovery document")
    p.add_argument(
        "--jwks-file",
        type=Path,
        help="jwt: the keys as a JWKS file (an issuer that serves them to no anonymous caller, "
        "such as a Kubernetes service-account issuer); re-read when it changes",
    )
    p.add_argument("--scope", action="append", default=[], help="jwt: a scope the token must hold")
    p.add_argument(
        "--allow-unauthenticated-network",
        action="store_true",
        help="none: allow a non-loopback --host (say what restricts the port)",
    )
    return p


def settings(argv: list[str] | None = None) -> tuple[Settings, argparse.Namespace]:
    a = _parser().parse_args(argv)
    jwt = None
    if a.auth == "jwt":
        if not a.issuer or not a.audience:
            raise SystemExit("tau-a2a: --auth jwt needs --issuer and --audience")
        if a.jwks_url and a.jwks_file:
            raise SystemExit("tau-a2a: --jwks-url or --jwks-file, not both")
        jwt = JwtSettings(
            issuer=a.issuer,
            audience=a.audience,
            jwks_url=a.jwks_url,
            jwks_file=a.jwks_file,
            scopes=a.scope,
        )
    elif not _loopback(a.host) and not a.allow_unauthenticated_network:
        raise SystemExit(
            f"tau-a2a: --auth none on {a.host} serves anyone who reaches the port; bind to "
            "loopback, use --auth jwt, or pass --allow-unauthenticated-network"
        )
    try:
        overlay = read_overlay(a.card_overlay) if a.card_overlay else None
    except (OSError, ValueError) as e:
        raise SystemExit(f"tau-a2a: --card-overlay: {e}") from e
    url = a.url or f"http://{a.host}:{a.port}/"
    s = Settings(
        socket=a.socket,
        db=a.db,
        url=url,
        name=a.name,
        version=a.version,
        fork_at=a.at,
        client_id=a.client_id,
        jwt=jwt,
        overlay=overlay,
        **({"description": a.description} if a.description else {}),
    )
    return s, a


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> None:
    s, a = settings(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    uvicorn.run(create_app(s), host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    main()
