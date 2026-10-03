"""The ``tau serve`` command (docs/TAU-SERVE.md §6).

``tau serve`` runs the daemon in the foreground and logs one line per event to
stdout. ``-d`` starts it in the background, logging to ``~/.tau/serve.log``,
and returns once it accepts connections. ``--tail`` is a client: it attaches to
a session and prints what happens to it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from websockets.asyncio.server import serve as ws_serve, unix_serve
from websockets.exceptions import InvalidHandshake
from websockets.sync.client import connect as sync_connect, unix_connect as sync_unix_connect

from tau_coding_agent.config import TAU_DIR
from tau_coding_agent.serve import protocol as p
from tau_coding_agent.serve.client import Address, ServeClient, ServeError, parse_address
from tau_coding_agent.serve.http import build_process_request

LOG_FILE = "serve.log"
"""The background daemon's log, under ``~/.tau``."""

READY_TIMEOUT_S = 30.0
"""How long ``-d`` waits for the daemon to accept a connection before it gives up."""


def build_parser() -> argparse.ArgumentParser:
    """The ``tau serve`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="tau serve",
        description="Serve sessions to clients over WebSocket (docs/TAU-SERVE.md).",
    )
    parser.add_argument(
        "--listen",
        metavar="ADDR",
        help=f"HOST[:PORT] or unix:/PATH (default: config serve.listen, else 127.0.0.1:{p.DEFAULT_PORT})",
    )
    parser.add_argument(
        "-d",
        "--daemon",
        action="store_true",
        help=f"run in the background, logging to ~/.tau/{LOG_FILE}",
    )
    parser.add_argument(
        "--tail",
        nargs="?",
        const="",
        metavar="SESSION",
        help="attach to SESSION (default: the newest) on a running daemon and print its events",
    )
    parser.add_argument(
        "--connect", metavar="ADDR", help="the daemon --tail dials (default: as --listen)"
    )
    return parser


def listen_address(flag: str | None, config: dict[str, Any]) -> Address:
    """The address to serve on: the flag, else ``serve.listen`` in config, else localhost."""
    text = flag or (config.get("serve") or {}).get("listen") or f"127.0.0.1:{p.DEFAULT_PORT}"
    return parse_address(str(text))


def run_serve(argv: list[str]) -> int:
    """Entry point for ``tau serve``; returns the process exit code."""
    from tau_coding_agent.config import load_config

    args = build_parser().parse_args(argv)
    config = load_config()
    try:
        address = listen_address(args.listen, config)
    except ValueError as exc:
        print(f"tau serve: {exc}", file=sys.stderr)
        return 2
    if args.tail is not None:
        target = parse_address(args.connect) if args.connect else address
        return asyncio.run(tail(target, args.tail or None))
    if args.daemon:
        return start_background(address, [a for a in argv if a not in ("-d", "--daemon")])
    return asyncio.run(serve(address, config))


async def serve(address: Address, config: dict[str, Any]) -> int:
    """Run the daemon in this process until SIGINT or SIGTERM."""
    from tau_coding_agent.serve.daemon import Daemon
    from tau_coding_agent.store_factory import build_session_catalog

    catalog = build_session_catalog(config, None, None, persist=True)
    daemon = Daemon(config, catalog)
    process_request = build_process_request(config.get("serve") or {}, daemon.log)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
    if address.path is not None:
        server = await unix_serve(
            daemon.handle, address.path, max_size=None, process_request=process_request
        )
    else:
        server = await ws_serve(
            daemon.handle,
            address.host,
            address.port,
            max_size=None,
            process_request=process_request,
        )
    auth = "token required" if daemon.token else "no token"
    daemon.log(f"tau serve listening on {address} ({auth}, pid {os.getpid()})")
    try:
        await stop.wait()
    finally:
        daemon.log("tau serve stopping")
        server.close()
        await server.wait_closed()
        await daemon.shutdown()
        from tau_llm.client import aclose_providers

        await aclose_providers()
    return 0


def start_background(address: Address, argv: list[str]) -> int:
    """Start ``tau serve`` detached, wait until it accepts connections, and return.

    The child is this interpreter running the same command without ``-d``, in
    its own session (its own process group on Windows), with stdout and stderr
    appended to ``~/.tau/serve.log``. If it exits before it accepts, its log
    tail is printed and this fails.
    """
    log_path = TAU_DIR / LOG_FILE
    log_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    start = log_path.stat().st_size if log_path.exists() else 0
    with log_path.open("a", encoding="utf-8") as log:
        child = subprocess.Popen(
            [sys.executable, "-m", "tau_coding_agent.cli", "serve", *argv],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if child.poll() is not None:
            print(
                f"tau serve: the daemon exited ({child.returncode}); {log_path}:", file=sys.stderr
            )
            print(_tail_of(log_path, start), file=sys.stderr)
            return 1
        if accepts(address):
            print(f"tau serve: listening on {address} (pid {child.pid}, log {log_path})")
            return 0
        time.sleep(0.1)
    print(
        f"tau serve: no answer on {address} after {READY_TIMEOUT_S:.0f}s; see {log_path}",
        file=sys.stderr,
    )
    return 1


def accepts(address: Address) -> bool:
    """Whether a WebSocket server completes a handshake at ``address`` right now.

    A full handshake rather than a bare TCP connect, which ``websockets`` logs
    as a failed handshake with a traceback.
    """
    try:
        if address.path is not None:
            ws = sync_unix_connect(address.path, uri=address.url, open_timeout=0.5)
        else:
            ws = sync_connect(address.url, open_timeout=0.5)
    except (OSError, TimeoutError, InvalidHandshake):
        return False
    ws.close()
    return True


def _tail_of(path: Path, start: int) -> str:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        handle.seek(start)
        return handle.read()[-4000:]


def describe(event: dict[str, Any]) -> str | None:
    """One line for a ``--tail`` event, or ``None`` for one not worth a line (a delta)."""
    kind = event["kind"]
    data = event["data"]
    seq = event["seq"]
    if kind in ("entry_open", "entry_append", "entry_final"):
        entry = data["entry"]
        what = entry.get("type")
        message = entry.get("message")
        if isinstance(message, dict):
            what = f"{what}/{message.get('role')}"
        return f"{seq:>6} {kind:<12} {entry['id']} {what}"
    if kind == "agent_event":
        if data.get("type") in ("message_update",):
            return None
        return f"{seq:>6} {data.get('type'):<12} cursor {data.get('cursor_id')}"
    if kind == "channel":
        return f"{seq:>6} {data['name']}"
    if kind == "cursors":
        states = ", ".join(
            f"{c['cursor_id']}@{c['leaf']}{'*' if c['busy'] else ''}" for c in data["cursors"]
        )
        return f"{seq:>6} cursors      {states}"
    return f"{seq:>6} {kind} {json.dumps(data)[:120]}"


async def tail(address: Address, session: str | None) -> int:
    """Attach to ``session`` and print its events, reconnecting with ``since`` when dropped."""
    client: ServeClient | None = None
    held: dict[str, Any] = {}
    delay = 0.5
    while True:
        try:
            client = await ServeClient.connect(
                address, client="tail", on_event=lambda e: _print_event(e)
            )
        except ServeError as exc:
            print(f"tau serve --tail: {exc}", file=sys.stderr)
            return 1
        except OSError as exc:
            if not held:
                print(f"tau serve --tail: no daemon at {address}: {exc}", file=sys.stderr)
                return 1
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)
            continue
        delay = 0.5
        client.replicas.update(held)
        if session is None:
            rows = (await client.request(p.ListSessions()))["sessions"]
            if not rows:
                print("tau serve --tail: the daemon has no sessions", file=sys.stderr)
                return 1
            session = rows[0]["id"]
        replica = await client.attach(session)
        print(
            f"attached to {replica.session_id[:8]} in {replica.cwd}: "
            f"{len(replica.entries)} entries, seq {replica.seq}",
            flush=True,
        )
        await client.closed.wait()
        held = dict(client.replicas)
        print(f"connection closed ({client.close_reason}); reconnecting", flush=True)


def _print_event(event: dict[str, Any]) -> None:
    line = describe(event)
    if line is not None:
        print(line, flush=True)
