"""`tau tokens`: the calibration records the `tokens` built-in keeps (D26 section 6.3).

    tau tokens show            every record under $TAU_HOME/calibration, one line each
    tau tokens show MODEL      one model's record, in words
    tau tokens reset MODEL     delete one model's record; it is learned again from the next replies

`MODEL` is a model's name in the config (`anthropic:opus`). The records are read and removed
whether or not the `tokens` extension is loaded; nothing here writes one.
"""

import argparse
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

from tau._core import Calibration, CalibrationError, CalibrationStore
from tau._startup import load_config
from tau._tokens import DIRECTORY, describe, record_name

ROLES = ("system", "user", "assistant", "calls", "results", "thinking")


HELP = "what tau has learned of each model's token counts"


def add_arguments(tokens: argparse.ArgumentParser) -> None:
    """Give the `tokens` command's parser its commands, `show` and `reset`."""
    tokens_sub = tokens.add_subparsers(dest="tokens_command", metavar="{show,reset}", required=True)
    show = tokens_sub.add_parser("show", help="the calibration records, or one model's in words")
    show.add_argument("model", nargs="?", metavar="MODEL", help="a model's name in the config")
    reset = tokens_sub.add_parser("reset", help="delete a model's calibration record")
    reset.add_argument("model", metavar="MODEL", help="a model's name in the config")


def _words(model: str, record: Calibration, path: Path) -> list[str]:
    provider, base_url, served = record.key
    where = f"{provider}, {served}" + (f" at {base_url}" if base_url else "")
    per_node = ", ".join(f"{role} {n}" for role, n in zip(ROLES, record.per_message, strict=True))
    markers = ", ".join(f"{k}={v}" for k, v in record.markers.items()) or "none"
    spans = f"{record.spans} proved {'span' if record.spans == 1 else 'spans'}"
    return [
        f"{model} ({where})",
        f"  file: {path}",
        f"  learned from: {spans}; last reply observed {record.updated or 'never'}",
        f"  counting: {describe(model, record, None).partition(': ')[2]}",
        f"  tokens per node: {per_node}",
        f"  drift markers: {markers}",
    ]


def _show_all(store: CalibrationStore, directory: Path, out: TextIO) -> int:
    names = store.names()
    if not names:
        out.write(f"no calibration records under {directory}\n")
        return 0
    for name in names:
        try:
            record = store.read(name)
        except CalibrationError as e:
            out.write(f"{name}: {e}\n")
            continue
        if record is not None:
            provider, base_url, served = record.key
            who = f"{provider} {served}" + (f" at {base_url}" if base_url else "")
            out.write(f"{name}: {describe(who, record, None).partition(': ')[2]} [{who}]\n")
    return 0


def command(
    args: argparse.Namespace, cwd: Path, environ: Mapping[str, str], out: TextIO, err: TextIO
) -> int:
    """Run `tau tokens show [MODEL]` or `tau tokens reset MODEL`; the exit status. `ConfigError`
    for a `MODEL` the config does not have."""
    loaded = load_config(cwd, environ, ask=None)
    directory = loaded.home / DIRECTORY
    store = CalibrationStore(directory)
    if args.model is None:
        return _show_all(store, directory, out)
    name = record_name(loaded.catalog.resolve(args.model))
    path = directory / name
    if args.tokens_command == "reset":
        removed = store.remove(name)
        out.write(f"removed {path}\n" if removed else f"no calibration record for {args.model}\n")
        return 0
    try:
        record = store.read(name)
    except CalibrationError as e:
        err.write(f"tau: {e}; `tau tokens reset {args.model}` removes it\n")
        return 1
    if record is None:
        out.write(f"no calibration record for {args.model} ({path})\n")
        return 0
    out.write("\n".join(_words(args.model, record, path)) + "\n")
    return 0
