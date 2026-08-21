"""Render a running Textual app to text, SVG, PNG, and a measured layout dump.

Four views of the same frame, cheapest first:

1. :func:`render_text` — the composited screen as a plain character grid. This is
   what you grep ("does the word Parley still appear?") and what you diff between
   two runs. It is the only view with no image dependency at all.
2. :func:`render_svg` — ``App.export_screenshot()``, colors included.
3. :func:`render_png` — the SVG rasterized, so a human or a coding agent can
   simply look at it.
4. :func:`dump_layout` — every widget's region, margin, padding, and border. For a
   layout complaint ("this doesn't fill the screen", "the padding is cluttered")
   the numbers say more than the picture does.

All four take an app that is already running inside ``App.run_test()``; none of
them start or stop anything. :func:`renderable_lines` is the odd one out: it takes
a lone Rich renderable and a width, for asserting on a widget's content in
isolation from the screen it happens to be on.

Reference: docs/textual-headless-testing.md
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from rich.console import Console, RenderableType
from textual.app import App
from textual.widget import Widget

__all__ = [
    "dump_layout",
    "render_png",
    "render_svg",
    "render_text",
    "renderable_lines",
    "save_render",
]


def _screen_render(app: App) -> Any:
    """The composited frame ``App.export_screenshot`` prints, including modals."""
    return app.screen._compositor.render_update(full=True, screen_stack=app._background_screens)


def render_text(app: App) -> str:
    """The current screen as a plain character grid, one line per terminal row.

    Same composited frame :meth:`App.export_screenshot` renders, exported as text
    instead of SVG. Trailing spaces are kept: column position is meaningful when
    you are looking for a widget that stops short of the screen edge.
    """
    width, height = app.size
    console = Console(
        width=width,
        height=height,
        file=io.StringIO(),
        force_terminal=True,
        color_system="truecolor",
        record=True,
        legacy_windows=False,
        safe_box=False,
    )
    console.print(_screen_render(app))
    return console.export_text()


def renderable_lines(renderable: RenderableType, width: int) -> list[str]:
    """One Rich renderable laid out at *width*, as plain character rows.

    The unit-level counterpart to :func:`render_text`: it answers "what does this
    widget's content look like in exactly N columns?" without mounting anything.
    Rows are returned un-stripped, so ``len(row) > width`` is a real overflow and
    not an artifact of the export.
    """
    console = Console(
        width=width,
        file=io.StringIO(),
        record=True,
        legacy_windows=False,
        safe_box=False,
    )
    console.print(renderable)
    return console.export_text().split("\n")[:-1]


def render_svg(app: App, *, title: str | None = None) -> str:
    """The current screen as an SVG string (``App.export_screenshot``)."""
    return app.export_screenshot(title=title)


#: What Rich writes into an exported SVG, and what has to be replaced before
#: cairosvg can rasterize it. Rich names a remote web font first and ``monospace``
#: as the fallback; cairosvg resolves the family name through cairo's toy font
#: API, which does not understand a comma-separated list, so it matches NEITHER
#: and lands on a proportional default with no box-drawing glyphs. Every border in
#: the screenshot then rasterizes as tofu.
_RICH_SVG_FONT = "font-family: Fira Code, monospace"

#: A locally installed monospace family that does have box-drawing glyphs. If it
#: is missing from the system, the PNG renders with tofu boxes instead of borders
#: — visible in the image itself, so it needs no separate check.
DEFAULT_PNG_FONT = "DejaVu Sans Mono"


def render_png(
    svg: str,
    path: Path,
    *,
    scale: float = 2.0,
    font: str = DEFAULT_PNG_FONT,
) -> Path:
    """Rasterize *svg* to a PNG at *path* and return the path.

    Requires ``cairosvg`` (the ``dev`` extra). A missing renderer raises here
    rather than skipping the PNG: a screenshot tool that silently produces no
    screenshot is worse than one that stops.
    """
    try:
        import cairosvg
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "render_png needs cairosvg; install the dev extra: "
            "pip install -e './tau-coding-agent[dev]'"
        ) from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    svg = svg.replace(_RICH_SVG_FONT, f"font-family: {font}")
    cairosvg.svg2png(bytestring=svg.encode("utf-8"), write_to=str(path), scale=scale)
    return path


def save_render(
    app: App,
    out_dir: Path,
    name: str,
    *,
    title: str | None = None,
    png: bool = True,
    layout: bool = True,
) -> dict[str, Path]:
    """Write every view of the current frame under ``out_dir/<name>.*``.

    Returns the paths actually written, keyed by extension.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    text_path = out_dir / f"{name}.txt"
    text_path.write_text(render_text(app))
    written["txt"] = text_path

    svg = render_svg(app, title=title or name)
    svg_path = out_dir / f"{name}.svg"
    svg_path.write_text(svg)
    written["svg"] = svg_path

    if png:
        written["png"] = render_png(svg, out_dir / f"{name}.png")

    if layout:
        layout_path = out_dir / f"{name}.layout.txt"
        layout_path.write_text(dump_layout(app))
        written["layout"] = layout_path

    return written


# ---------------------------------------------------------------------------
# Layout measurement
# ---------------------------------------------------------------------------


def _spacing(value: Any) -> str:
    """A Textual ``Spacing`` as ``t,r,b,l``, or ``.`` when it is all zeroes."""
    if value is None:
        return "."
    edges = (value.top, value.right, value.bottom, value.left)
    if not any(edges):
        return "."
    return ",".join(str(edge) for edge in edges)


def _border(value: Any) -> str:
    """A border definition as one word when uniform, else per-edge."""
    if not value:
        return "."
    edges = [(edge[0] or "") for edge in value]
    if not any(edges):
        return "."
    if len(set(edges)) == 1:
        return edges[0]
    names = ("t", "r", "b", "l")
    return " ".join(f"{n}:{e}" for n, e in zip(names, edges) if e)


def _identity(widget: Widget) -> str:
    name = type(widget).__name__
    if widget.id:
        name += f"#{widget.id}"
    classes = [c for c in widget.classes if not c.startswith("-")]
    if classes:
        name += "." + ".".join(sorted(classes))
    return name


def _row(widget: Widget, depth: int) -> str:
    region = widget.region
    styles = widget.styles
    flags = []
    if styles.display == "none":
        flags.append("display:none")
    if not widget.visible:
        flags.append("hidden")
    if region.width == 0 and region.height == 0:
        flags.append("not-laid-out")
    scroll = ""
    if widget.is_scrollable:
        virtual = widget.virtual_size
        if virtual.height > region.height:
            scroll = f" scroll={virtual.height}/{region.height}"
    return (
        f"{'  ' * depth}{_identity(widget)}\n"
        f"{'  ' * depth}  xywh={region.x},{region.y},{region.width},{region.height}"
        f"  margin={_spacing(styles.margin)}"
        f"  padding={_spacing(styles.padding)}"
        f"  border={_border(styles.border)}"
        f"{scroll}"
        f"{('  ' + ' '.join(flags)) if flags else ''}"
    )


def _walk(widget: Widget, depth: int, lines: list[str], max_depth: int | None) -> None:
    lines.append(_row(widget, depth))
    if max_depth is not None and depth >= max_depth:
        return
    for child in widget.children:
        _walk(child, depth + 1, lines, max_depth)


def dump_layout(
    app: App,
    *,
    root: Widget | None = None,
    max_depth: int | None = None,
) -> str:
    """A measured widget tree: region, margin, padding, border, scroll, per node.

    ``root`` defaults to the active screen, so a modal dumps the modal. A node
    reported as ``not-laid-out`` has a zero region — it is mounted but the
    compositor gave it no space, which is itself usually the bug you are chasing.
    """
    target: Widget = root if root is not None else app.screen
    lines: list[str] = [f"# {app.size.width}x{app.size.height}  screen={type(app.screen).__name__}"]
    _walk(target, 0, lines, max_depth)
    return "\n".join(lines) + "\n"


# A ``chrome_cost(widgets) -> str`` used to sit here: per widget, the rows spent
# on margin + border + padding versus the rows left for content. It was written
# for the density pass, `dump_layout` was used instead, and it was never called
# again — deleted rather than given a caller, for two reasons.
#
# It measures nothing `dump_layout` does not already print: same margin, same
# padding, same border, off the same `styles`, plus the region and scroll extent
# it left out. And a test cannot use it as it stands, because what it returns is
# a *report*. Asserting on chrome overhead through a formatted string means
# parsing the string; the assertions it was meant to serve already exist in
# `test_tui_appearance.py` and read the numbers straight off the widget —
# `test_a_collapsed_collapsible_is_one_row` is the vertical case and
# `test_chat_text_gets_most_of_the_column` the horizontal one.
