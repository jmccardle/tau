"""Render τ's TUI scenes to text, SVG, PNG, and layout dumps.

The iteration loop this exists for:

1. Run ``python -m tau_coding_agent.devshot --scene tools --size 120x40``.
2. Look at ``shots/tools@120x40.png``.
3. Edit ``parley.tcss``.
4. Repeat step 1.

Nothing here touches a real terminal, a real ``~/.tau``, or the network. Scenes
live in :mod:`tau_coding_agent.testing.scenes`; rendering lives in
:mod:`tau_coding_agent.testing.render`.

Reference: docs/textual-headless-testing.md
"""

from __future__ import annotations

import os

# Textual reads TEXTUAL_ANIMATIONS once, at import time, so this must run before
# anything imports textual. A moving frame is a frame that screenshots differently
# on every run.
os.environ.setdefault("TEXTUAL_ANIMATIONS", "none")

import argparse  # noqa: E402
import asyncio  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

from tau_coding_agent.testing.render import save_render  # noqa: E402
from tau_coding_agent.testing.scenes import SCENES, Scene, get_scene, open_scene  # noqa: E402

DEFAULT_SIZES = ((120, 40),)


def parse_size(text: str) -> tuple[int, int]:
    """``"120x40"`` -> ``(120, 40)``."""
    try:
        width, height = text.lower().split("x", 1)
        return int(width), int(height)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"size must look like 120x40, got {text!r}") from exc


async def shoot(
    scene: Scene,
    size: tuple[int, int],
    out_dir: Path,
    *,
    png: bool = True,
) -> dict[str, Path]:
    """Render one scene at one size; return the files written."""
    async with open_scene(scene, size) as (app, _pilot):
        name = f"{scene.name}@{size[0]}x{size[1]}"
        return save_render(app, out_dir, name, title=f"tau — {scene.name}", png=png)


async def run(
    scenes: list[Scene],
    sizes: list[tuple[int, int]],
    out_dir: Path,
    *,
    png: bool,
) -> None:
    for scene in scenes:
        for size in sizes:
            written = await shoot(scene, size, out_dir, png=png)
            target = written.get("png") or written["svg"]
            print(f"{scene.name}@{size[0]}x{size[1]}  ->  {target}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tau_coding_agent.devshot",
        description="Render τ TUI scenes headlessly to text/SVG/PNG/layout dumps.",
    )
    parser.add_argument(
        "--scene",
        action="append",
        dest="scenes",
        metavar="NAME",
        help="scene to render (repeatable); default: all",
    )
    parser.add_argument(
        "--size",
        action="append",
        dest="sizes",
        type=parse_size,
        metavar="WxH",
        help="terminal size (repeatable); default: 120x40",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("shots"),
        help="output directory (default: ./shots)",
    )
    parser.add_argument("--no-png", action="store_true", help="skip PNG rasterization")
    parser.add_argument("--list", action="store_true", help="list scene names and exit")
    args = parser.parse_args(argv)

    if args.list:
        for scene in SCENES:
            print(f"{scene.name:16} {scene.description}")
        return 0

    try:
        scenes = [get_scene(name) for name in args.scenes] if args.scenes else list(SCENES)
    except KeyError as exc:
        parser.error(str(exc.args[0]))

    sizes = args.sizes or list(DEFAULT_SIZES)
    asyncio.run(run(scenes, sizes, args.out, png=not args.no_png))
    return 0


if __name__ == "__main__":
    sys.exit(main())
