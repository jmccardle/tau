"""``tau ext``: list, authorize, revoke, disable and enable extensions, with no session and no model.

Rationale: docs/EXTENSION-PACKAGES.md §7 (the listing is one core function
every head renders) and §9 (the non-interactive route to the gate).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import Sequence

from tau_agent_core.extension_auth import AuthorizationStoreError
from tau_agent_core.extension_sources import ExtensionRecord
from tau_agent_core.sdk import (
    authorize_discovered_extension,
    disable_discovered_extension,
    enable_discovered_extension,
    extension_records,
    is_extension_disabled,
    revoke_extension_authorization,
)


def render_extension_records(records: Sequence[ExtensionRecord]) -> list[str]:
    """The ``tau ext list`` lines: one per record, then an indented reason.

    Args:
        records: From :func:`~tau_agent_core.sdk.extension_records` or
            :meth:`AgentSession.extension_records`.

    Returns:
        The lines; ``["no extensions found"]`` for none.
    """
    if not records:
        return ["no extensions found"]
    lines = []
    for record in records:
        if record.kind == "package":
            where = f"{record.distribution} {record.version} (entry point {record.entry_point})"
        else:
            where = record.target
        lines.append(f"{record.identifier}  {record.state}  {record.kind}  {where}")
        if record.reason:
            lines.append(f"    {record.reason}")
        for topic in record.orphan_topics:
            lines.append(f"    subscribes to {topic}, which no loaded extension emits")
    return lines


def _build_parser() -> argparse.ArgumentParser:
    """The ``tau ext`` parser and its five verbs."""
    parser = argparse.ArgumentParser(
        prog="tau ext",
        description=(
            "List, authorize, revoke, disable and enable τ extensions "
            "(docs/EXTENSION-PACKAGES.md). A disabled extension is found but never "
            "loaded or announced, until enabled or named with -e."
        ),
    )
    verbs = parser.add_subparsers(dest="verb", required=True)
    listing = verbs.add_parser(
        "list", help="every extension found, and whether it would load; imports nothing"
    )
    listing.add_argument(
        "--extension",
        "-e",
        dest="extensions",
        action="append",
        default=[],
        metavar="PATH|MODULE",
        help="include an extension as `tau -e` would (repeatable)",
    )
    listing.add_argument("--json", action="store_true", help="one JSON object per line")
    grant = verbs.add_parser(
        "authorize", help="authorize a discovered extension so later starts load it"
    )
    grant.add_argument("name", help="an identifier, its last dotted segment, or a drop-in's path")
    grant.add_argument(
        "--package",
        action="store_true",
        help="when a drop-in and a package share the name, choose the package",
    )
    revoke = verbs.add_parser("revoke", help="remove every authorization record for IDENTIFIER")
    revoke.add_argument("identifier", help="the full identifier")
    disable = verbs.add_parser(
        "disable",
        help="stop loading a discovered extension, and stop its notices; -e still loads it",
    )
    disable.add_argument("name", help="an identifier, its last dotted segment, or a drop-in's path")
    enable = verbs.add_parser("enable", help="undo `tau ext disable`")
    enable.add_argument("name", help="a disabled identifier or its last dotted segment")
    return parser


def run_ext(argv: Sequence[str]) -> int:
    """Run ``tau ext <verb> …`` and return the exit status.

    Args:
        argv: The arguments after ``ext``.

    Returns:
        0 on success, 2 when the name resolves to nothing or the store is unreadable.
    """
    args = _build_parser().parse_args(list(argv))
    try:
        if args.verb == "list":
            records = extension_records(args.extensions)
            if args.json:
                for record in records:
                    print(
                        json.dumps({**asdict(record), "orphan_topics": list(record.orphan_topics)})
                    )
            else:
                print("\n".join(render_extension_records(records)))
            return 0
        if args.verb == "authorize":
            source = authorize_discovered_extension(
                args.name, kind="package" if args.package else None
            )
            detail = (
                f"{source.distribution} {source.version}"
                if source.kind == "package"
                else source.target
            )
            if is_extension_disabled(source.identifier):
                print(
                    f"authorized {source.identifier} ({detail}); it is disabled, so it won't "
                    f"load until `tau ext enable {source.identifier}`"
                )
            else:
                print(f"authorized {source.identifier} ({detail}); it loads at the next start")
            return 0
        if args.verb == "disable":
            identifier, changed = disable_discovered_extension(args.name)
            if changed:
                print(f"disabled {identifier}; it is not loaded or announced from the next start")
            else:
                print(f"{identifier} was already disabled")
            return 0
        if args.verb == "enable":
            identifier = enable_discovered_extension(args.name)
            print(f"enabled {identifier}; it loads at the next start if it is authorized")
            return 0
        removed = revoke_extension_authorization(args.identifier)
        if not removed:
            print(f"tau ext: no authorization record names {args.identifier!r}", file=sys.stderr)
            return 2
        print(f"revoked {removed} authorization record(s) for {args.identifier}")
        return 0
    except (ValueError, AuthorizationStoreError) as exc:
        print(f"tau ext: error: {exc}", file=sys.stderr)
        return 2
