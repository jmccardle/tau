"""The screens τ's TUI is built on: Textual's, that do not find a widget which has left the DOM.

Textual answers "what is under the pointer" from the map of the last paint. Our transcript removes
widgets between paints (a streaming `Markdown` replaces its last block, a committed reply takes
its live box away, the window drops blocks as it slides), and Textual's `Screen._forward_event`
reads `widget.parent` of the answer on a mouse down, which is `None` for a removed widget. A
widget that is no longer attached is not under the pointer: the answer is the one Textual gives
for nothing at that point.

The two overrides are written once, as `attached_at` and `attached_and_offset_at`, and each screen
class (`TauScreen`, and `TauDialog` over `ModalScreen`) calls them: a shared base class would
come before `ModalScreen` in the bases, and Textual takes a screen's CSS type names from the
first base only.
"""

from textual.errors import NoWidget
from textual.geometry import Offset, Region
from textual.screen import Screen
from textual.widget import Widget


def attached_at(found: tuple[Widget, Region], x: int, y: int) -> tuple[Widget, Region]:
    """`found`, Textual's answer for the point (x, y), unless its widget has left the DOM, which
    raises `NoWidget` as Textual does for a point with nothing at it."""
    if not found[0].is_attached:
        raise NoWidget(f"No attached widget under screen coordinate ({x}, {y})")
    return found


def attached_and_offset_at(
    found: tuple[Widget | None, Offset | None],
) -> tuple[Widget | None, Offset | None]:
    """`found`, Textual's answer for a point, unless its widget has left the DOM, which is
    `(None, None)` as Textual answers for a point with nothing at it."""
    if found[0] is not None and not found[0].is_attached:
        return None, None
    return found


class TauScreen(Screen[None]):
    """The app's default screen, the transcript's: a widget removed since the last paint is not
    under the pointer."""

    def get_widget_at(self, x: int, y: int) -> tuple[Widget, Region]:
        return attached_at(super().get_widget_at(x, y), x, y)

    def get_widget_and_offset_at(self, x: int, y: int) -> tuple[Widget | None, Offset | None]:
        return attached_and_offset_at(super().get_widget_and_offset_at(x, y))
