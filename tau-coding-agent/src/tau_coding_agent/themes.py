"""τ's colour themes: the palette half of the TUI's appearance.

Reference: docs/PLAN-0.9.4.md §6 ("Swappable TCSS themes").

Why this is not a second ``parley.tcss``
----------------------------------------
The obvious way to ship themes is one whole stylesheet per theme. It cannot get
out of sync — and it costs a thousand-line copy per theme, of which ~975 lines
are structure (widths, paddings, the markdown de-spacing block, the collapsible
density rules) that no theme has any business restating. The second theme would
be a fork of the first, and the third would be a fork of whichever fork was
younger.

So the split is **structure in one stylesheet, colour in a palette**, and the
palette mechanism is the one Textual already ships: :class:`textual.theme.Theme`.
Its ``variables`` mapping is merged into the CSS variable namespace by
``App.get_css_variables``, so ``parley.tcss`` can say ``color: $tau-text`` and a
theme decides what that is. Setting ``App.theme`` re-runs
``refresh_css`` — a live swap, no restart, no stylesheet reloading of our own.

Using Textual's ``Theme`` rather than a bare ``dict`` of variables buys the half
of a theme that ``parley.tcss`` does **not** reach: Textual's own widgets
(``Footer``, ``Header``, scrollbars, ``Toast``, ``Tree``'s cursor line, ``Button``
defaults) are coloured from the design tokens ``$primary``/``$surface``/
``$background``/``dark``. A light τ palette laid over a dark Textual base leaves
a dark Footer under a light chat pane. A ``Theme`` carries both halves at once,
which is exactly the pairing a theme is.

The split is verifiable, not aspirational: ``tests/test_themes.py`` fails if a
colour literal appears anywhere in ``parley.tcss``, which is the only thing that
keeps "structure has no colour" true a year from now.

The default is load-bearing
---------------------------
:data:`DEFAULT_THEME_NAME` is ``"mocha"`` and it must render **byte-identically**
to the pre-theme TUI, because ``tests/test_tui_snapshots.py`` compares seven
composited screens against committed SVGs. Two things make that true:

1. Every ``$tau-*`` value in ``_MOCHA_PALETTE`` is the hex literal that stood at
   that spot in ``parley.tcss`` before the variables landed.
2. ``mocha``'s *design tokens* are Textual's ``textual-dark`` tokens exactly —
   ``textual-dark`` is ``App``'s default theme, so this is what the app was
   already running. ``test_themes.py`` re-derives that from
   ``textual.theme.BUILTIN_THEMES`` rather than trusting the copy, so a Textual
   upgrade that restyles ``textual-dark`` fails here instead of silently in a
   snapshot diff.

Fail Early, and where the app stops applying it
-----------------------------------------------
Nothing in this module falls back to the default theme. A name that is not
registered raises and lists what is; a user theme file that will not parse raises
and names the file; a palette key that is not part of the vocabulary raises and
names it (a typo that were merely ignored would be a colour the user set and
never got).

The **app** does not want that shape at startup, and the difference is the one
``~/.claude/CLAUDE.md`` draws: Fail Early is about not hiding a problem, not
about manufacturing one. A single unparseable file in ``~/.tau/themes`` would
otherwise stop τ from starting at all — including when the theme in use is a
built-in and the broken file is one the user is not even selecting. So
:func:`build_theme_registry` and :func:`load_user_themes` take an ``errors``
list: pass one and a bad file is *collected and skipped* instead of raised, for
the caller to report. ``Parley`` passes one and turns each entry into an error
toast, then runs in the default theme. The problem is still shown — on the one
screen the user is looking at — and τ still starts.

Passing no ``errors`` list keeps the raising contract, which is what a test and
any non-interactive caller want.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from textual.theme import BUILTIN_THEMES, Theme

from tau_coding_agent.config import ConfigError

__all__ = [
    "DEFAULT_THEME_NAME",
    "TAU_PALETTE_KEYS",
    "THEME_CONFIG_KEY",
    "ThemeError",
    "build_theme_registry",
    "install_themes",
    "load_user_themes",
    "resolve_theme",
    "tau_themes",
]

#: The ``~/.tau/config.json`` key holding the standing choice. One flat top-level
#: key, like ``default_model`` and ``system_prompt`` beside it.
THEME_CONFIG_KEY = "theme"

DEFAULT_THEME_NAME = "mocha"

#: Where a user's own themes live. A *relative* name resolved against
#: ``config.CONFIG_PATH.parent`` at call time, never against ``config.TAU_DIR``:
#: ``TAU_DIR`` is frozen at import and ``testing.sandbox.sandbox_tau_home`` only
#: redirects ``CONFIG_PATH``, so reading ``TAU_DIR`` here is how a "hermetic" test
#: quietly starts loading the developer's real themes.
USER_THEME_DIRNAME = "themes"


class ThemeError(ConfigError):
    """A theme that cannot be resolved, read, or understood.

    Subclasses :class:`~tau_coding_agent.config.ConfigError` so ``cli.main``'s
    existing handler reports it as the configuration problem it is, rather than
    as a traceback.
    """


# ---------------------------------------------------------------------------
# The palette vocabulary
# ---------------------------------------------------------------------------
#
# Every colour ``parley.tcss`` can name, and nothing else. The names are ROLES
# ("what is this colour for") rather than hues ("mauve"), because a role survives
# a palette that has no mauve in it. Where the old stylesheet deliberately reused
# one hue for two things — the branch-summary pair borrowing the user cyan, the
# hover divergence borrowing the assistant amber — the rules now share the *role*
# variable, so the pairing those comments describe survives a theme swap by
# construction instead of by two hex literals happening to agree.
#
# The six ``text-*`` entries are a monotone ramp, brightest first. A theme that
# breaks the ordering is not rejected (some palettes genuinely have no six steps)
# but it will read as noise: the stylesheet uses position in the ramp to say how
# loud a piece of chrome is.

TAU_PALETTE_KEYS: tuple[str, ...] = (
    # Surfaces, back to front.
    "bg",  # the chat pane and every inner field
    "bg-alt",  # sidebar, dialogs, docked strips
    "bg-deep",  # code blocks, the sidebar's own title bar
    "surface",  # a raised control (button, hovered list row)
    "surface-hover",  # that control under the pointer
    # Lines.
    "border",  # the default border on everything
    "border-subtle",  # a divider that should not read as a border
    "accent",  # dialog titles, focused field borders, panel titles
    # Text, brightest to faintest.
    "text",
    "text-soft",
    "text-dim",
    "text-quiet",
    "text-muted",
    "text-faint",
    # Message roles. These carry meaning through colour, which is the whole
    # reason a single-accent brand palette is hard here (docs/PLAN-0.9.4.md §6).
    "role-user",
    "role-assistant",
    "role-system",
    "role-tool",
    "role-result",
    "role-error",
    "role-blocked",
    "role-foreign",
    "role-pending",
    # The one tree-browser zone with no role to borrow: "on the cursor's
    # ancestry" is about position, not about a transformation.
    "zone-path",
    # Code inside a markdown fence.
    "code-fg",
)

_PALETTE_KEY_SET = frozenset(TAU_PALETTE_KEYS)

#: Textual ``Theme`` fields a user theme file may override. ``name`` is not one of
#: them — the file name is the theme's name, and a file that disagreed with itself
#: would register under a name nobody could type.
_TEXTUAL_THEME_FIELDS: frozenset[str] = frozenset(
    {
        "primary",
        "secondary",
        "warning",
        "error",
        "success",
        "accent",
        "foreground",
        "background",
        "surface",
        "panel",
        "boost",
        "dark",
        "luminosity_spread",
        "text_alpha",
        "variables",
        "ansi",
    }
)


def _variables(palette: Mapping[str, str]) -> dict[str, str]:
    """``{"bg": "#1e1e2e"}`` -> ``{"tau-bg": "#1e1e2e"}``, completeness enforced.

    The prefix is what keeps τ's vocabulary out of Textual's: ``$background`` is
    Textual's and means "the app's base colour"; ``$tau-bg`` is ours and means
    "the colour the chat pane is painted".
    """
    missing = sorted(_PALETTE_KEY_SET - set(palette))
    if missing:
        raise ThemeError(
            "theme palette is missing " + ", ".join(missing) + ". Every key in "
            "themes.TAU_PALETTE_KEYS must have a colour: parley.tcss references "
            "all of them, and an absent one is a CSS parse error at startup, not "
            "a colour that quietly stays the same."
        )
    unknown = sorted(set(palette) - _PALETTE_KEY_SET)
    if unknown:
        raise ThemeError(
            "theme palette has no such key: "
            + ", ".join(unknown)
            + ". Valid keys: "
            + ", ".join(TAU_PALETTE_KEYS)
        )
    return {f"tau-{key}": value for key, value in palette.items()}


# ---------------------------------------------------------------------------
# The built-in themes
# ---------------------------------------------------------------------------

#: Catppuccin Mocha — τ's look since the fork, and the default. Every value here
#: is the literal that stood at that spot in ``parley.tcss`` before this module
#: existed; the snapshot suite is the proof.
_MOCHA_PALETTE: dict[str, str] = {
    "bg": "#1e1e2e",  # base
    "bg-alt": "#181825",  # mantle
    "bg-deep": "#11111b",  # crust
    "surface": "#313244",  # surface0
    "surface-hover": "#45475a",  # surface1
    "border": "#45475a",  # surface1
    "border-subtle": "#313244",  # surface0
    "accent": "#cba6f7",  # mauve
    "text": "#cdd6f4",  # text
    "text-soft": "#bac2de",  # subtext1
    "text-dim": "#a6adc8",  # subtext0
    "text-quiet": "#9399b2",  # overlay2
    "text-muted": "#6c7086",  # overlay0
    "text-faint": "#585b70",  # surface2
    "role-user": "#89dceb",  # sky
    "role-assistant": "#f9e2af",  # yellow
    "role-system": "#cba6f7",  # mauve
    "role-tool": "#fab387",  # peach
    "role-result": "#a6e3a1",  # green
    "role-error": "#f38ba8",  # red
    "role-blocked": "#f9e2af",  # yellow — a policy veto, not a failure
    "role-foreign": "#b4a7f5",  # lavender-mauve, adjacent to role-system
    "role-pending": "#6c7086",  # overlay0
    "zone-path": "#89b4fa",  # blue
    "code-fg": "#a6e3a1",  # green
}

#: Catppuccin Latte — the light counterpart of the default, and the answer to the
#: one obviously unserved case: a light terminal, where the whole app was
#: previously a dark rectangle pasted onto a white screen. Same hue *family* per
#: role as Mocha (sky→sky, yellow→yellow, peach→peach), so a user who knows what
#: an amber border means keeps knowing.
#:
#: The surfaces and the text ramp are Catppuccin Latte verbatim. **The role hues
#: are not**, and that is the one real piece of design in this theme: Latte's
#: published accents are tuned to be borders on a light base, so its yellow
#: (#df8e1d) is 2.3:1 against the chat pane and its peach (#fe640b) is 2.6:1 —
#: and τ does not use these as borders only. ``.tool-box > CollapsibleTitle``
#: paints a whole title row in ``$tau-role-tool``, ``Markdown CodeBlock`` paints
#: the code itself in ``$tau-code-fg``. Every role is therefore darkened to clear
#: 3:1 against the pane, which ``test_themes.py`` re-measures. Dark themes get
#: this for free (a saturated hue on near-black is high-contrast by
#: construction); a light theme is where the borrowed-palette shortcut stops
#: working, so it is the one that had to be tuned.
_LATTE_PALETTE: dict[str, str] = {
    "bg": "#eff1f5",  # base
    "bg-alt": "#e6e9ef",  # mantle
    "bg-deep": "#dce0e8",  # crust
    "surface": "#ccd0da",  # surface0
    "surface-hover": "#bcc0cc",  # surface1
    "border": "#acb0be",  # surface2
    "border-subtle": "#ccd0da",  # surface0
    "accent": "#8839ef",  # mauve
    "text": "#4c4f69",  # text
    "text-soft": "#5c5f77",  # subtext1
    "text-dim": "#6c6f85",  # subtext0
    "text-quiet": "#7c7f93",  # overlay2
    "text-muted": "#8c8fa1",  # overlay1
    "text-faint": "#9ca0b0",  # overlay0
    "role-user": "#0e6f8a",  # sky, deepened
    "role-assistant": "#9a6410",  # yellow, deepened
    "role-system": "#8839ef",  # mauve (already 4.8:1)
    "role-tool": "#b34a05",  # peach, deepened
    "role-result": "#2f761f",  # green, deepened
    "role-error": "#d20f39",  # red (already 4.8:1)
    "role-blocked": "#9a6410",  # yellow, deepened
    "role-foreign": "#4553c9",  # lavender, deepened to indigo
    "role-pending": "#7c7f93",  # overlay2 — one step quieter than body text,
    # which is where Mocha's pending grey sits too
    "zone-path": "#1e66f5",  # blue (already 4.3:1)
    "code-fg": "#2f761f",  # green, deepened
}

#: Two of Textual's own variables that Latte's design tokens derive badly, found
#: by looking at a render rather than by reading a palette (docs/PLAN-0.9.4.md §6
#: acceptance step 6):
#:
#: * the scrollbar thumb defaults off ``$primary``, which for Catppuccin Latte is
#:   a saturated mauve — on a light background a full-height bar of it is the
#:   loudest thing on the screen, louder than any message border, which inverts
#:   the hierarchy the whole palette is arranged around.
#: * the Footer's key labels default off ``$accent``, Latte's peach #fe640b, which
#:   is 2.6:1 on the Footer's panel. The deepened peach reads and keeps the hue.
_LATTE_TEXTUAL_VARS: dict[str, str] = {
    "scrollbar": "#acb0be",  # surface2 — present, quiet
    "scrollbar-hover": "#9ca0b0",  # overlay0
    "scrollbar-active": "#8c8fa1",  # overlay1
    "scrollbar-background": "#e6e9ef",  # mantle
    "scrollbar-background-hover": "#e6e9ef",
    "scrollbar-background-active": "#e6e9ef",
    "scrollbar-corner-color": "#e6e9ef",
    "footer-key-foreground": "#b34a05",
}

#: Gruvbox Dark — the third theme exists to be *unlike* the first two rather than
#: to be a third pastel. Warm, higher contrast, retro-terminal: where Mocha and
#: Latte are the same cool palette at two luminosities, this one is a different
#: temperature entirely, which is the axis a reader picks a theme on.
#:
#: Gruvbox has exactly one purple, so ``accent``, ``role-system`` and
#: ``role-foreign`` all land on it. That is a real (small) loss against Mocha,
#: where the foreign lane's lavender is a shade off the system mauve — the lane
#: is still unmistakable because its border is ``dashed`` and indented, which is
#: structure, not colour. It is also the miniature of the FFwF problem: a palette
#: with fewer hues than the UI has roles has to spend one twice.
_GRUVBOX_PALETTE: dict[str, str] = {
    "bg": "#282828",  # bg0
    "bg-alt": "#1d2021",  # bg0_hard
    "bg-deep": "#141617",  # below bg0_hard: fences need to sit under bg-alt too
    "surface": "#3c3836",  # bg1
    "surface-hover": "#504945",  # bg2
    "border": "#504945",  # bg2
    "border-subtle": "#3c3836",  # bg1
    "accent": "#d3869b",  # purple
    "text": "#ebdbb2",  # fg1
    "text-soft": "#d5c4a1",  # fg2
    "text-dim": "#bdae93",  # fg3
    "text-quiet": "#a89984",  # fg4
    "text-muted": "#928374",  # gray
    "text-faint": "#665c54",  # bg3
    "role-user": "#8ec07c",  # aqua
    "role-assistant": "#fabd2f",  # yellow
    "role-system": "#d3869b",  # purple
    "role-tool": "#fe8019",  # orange
    "role-result": "#b8bb26",  # green
    "role-error": "#fb4934",  # red
    "role-blocked": "#fabd2f",  # yellow
    "role-foreign": "#d3869b",  # purple (see the note above)
    "role-pending": "#928374",  # gray
    "zone-path": "#83a598",  # blue
    "code-fg": "#b8bb26",  # green
}


#: ANSI — the only theme that fits a terminal τ has never seen.
#:
#: Every value is an ANSI colour *name* rather than a hex literal, so the 16
#: colours the user already curated in their terminal emulator decide what τ looks
#: like. Textual resolves these to ``Color(..., ansi=n)`` and emits the ANSI code
#: rather than a truecolor escape, which is what makes that true.
#:
#: **It paints no backgrounds.** Every surface is ``ansi_default`` — the
#: terminal's own background — and all of τ's structure is carried by foreground
#: hues and borders. That is not minimalism; it is the only design that is correct
#: in a scheme whose direction is unknown. ``ansi_black`` is a black sidebar on a
#: light terminal and invisible on a dark one, so no surface can name it.
#:
#: For the same reason the roles use the *non-bright* half of the palette: a
#: terminal tunes its normal six to be readable against its own background, and
#: tunes the bright six to stand out against the normal ones. Bright is used only
#: where a role needs a second shade of a hue already spent (``role-tool`` beside
#: ``role-assistant``, ``role-foreign`` beside ``role-system``) — the same pairs
#: Mocha spends two adjacent hues on.
#:
#: Two costs, on the record, both consequences of having 16 colours where the
#: other themes have 24 bits:
#:
#: * The six-step text ramp collapses to three. ``text``/``text-soft`` are the
#:   terminal's foreground and ``text-dim`` down to ``text-faint`` are all
#:   ``ansi_bright_black``, because grey is the only quieter step ANSI has.
#: * ``border`` and ``border-subtle`` are the same colour, so the divider that is
#:   meant not to read as a border does read as one.
#:
#: ``dark=True`` is inherited from Textual's ``ansi-dark`` and is the one thing
#: here that *is* a guess — it selects Textual's dark branch for its own widgets.
#: A light-terminal user overrides it without a new palette, which is what the
#: user-theme format is for::
#:
#:     ~/.tau/themes/ansi-light.json
#:     { "extends": "ansi", "textual": { "dark": false } }
_ANSI_PALETTE: dict[str, str] = {
    "bg": "ansi_default",
    "bg-alt": "ansi_default",
    "bg-deep": "ansi_default",
    "surface": "ansi_default",
    "surface-hover": "ansi_default",
    "border": "ansi_bright_black",
    "border-subtle": "ansi_bright_black",
    "accent": "ansi_magenta",
    "text": "ansi_default",
    "text-soft": "ansi_default",
    "text-dim": "ansi_bright_black",
    "text-quiet": "ansi_bright_black",
    "text-muted": "ansi_bright_black",
    "text-faint": "ansi_bright_black",
    "role-user": "ansi_cyan",
    "role-assistant": "ansi_yellow",
    "role-system": "ansi_magenta",
    "role-tool": "ansi_bright_yellow",  # a second yellow, beside the assistant's
    "role-result": "ansi_green",
    "role-error": "ansi_red",
    "role-blocked": "ansi_yellow",  # a policy veto, not a failure — as in Mocha
    "role-foreign": "ansi_bright_magenta",  # adjacent to role-system, as in Mocha
    "role-pending": "ansi_bright_black",
    "zone-path": "ansi_blue",
    "code-fg": "ansi_green",
}


def _builtin_tau_themes() -> dict[str, Theme]:
    """Construct the built-in τ themes.

    A function rather than a module-level dict so each caller gets its own
    ``Theme`` objects: ``App.register_theme`` stores them by reference and
    ``Theme`` is a plain (mutable) dataclass, so a shared instance is a shared
    palette between two concurrently-running apps.
    """
    return {
        "mocha": Theme(
            name="mocha",
            # ``textual-dark``'s design tokens, verbatim. Not a stylistic
            # choice: ``textual-dark`` is ``App``'s default, so these are the
            # tokens the TUI has always run under, and any other value here
            # would recolour the Footer, the scrollbars and the Tree cursor —
            # i.e. break the snapshot suite. ``test_themes.py`` re-derives them
            # from ``textual.theme.BUILTIN_THEMES`` so a Textual upgrade that
            # restyles ``textual-dark`` is caught here.
            primary="#0178D4",
            secondary="#004578",
            accent="#ffa62b",
            warning="#ffa62b",
            error="#ba3c5b",
            success="#4EBF71",
            foreground="#e0e0e0",
            dark=True,
            variables=_variables(_MOCHA_PALETTE),
        ),
        "latte": _derive("latte", "catppuccin-latte", _LATTE_PALETTE, _LATTE_TEXTUAL_VARS),
        "gruvbox": _derive("gruvbox", "gruvbox", _GRUVBOX_PALETTE),
        "ansi": _derive("ansi", "ansi-dark", _ANSI_PALETTE),
    }


def _derive(
    name: str,
    base: str,
    palette: Mapping[str, str],
    extra: Mapping[str, str] | None = None,
) -> Theme:
    """A τ theme carrying *base*'s Textual design tokens and *palette*'s colours.

    The tokens colour everything ``parley.tcss`` never mentions; the palette
    colours everything it does. Both halves have to move together or the app is
    two themes at once.

    ``extra`` is for Textual's *own* named variables (``scrollbar``,
    ``footer-key-foreground``, …) where the base theme's derived default is wrong
    for τ. These are not part of τ's vocabulary and no rule in ``parley.tcss``
    names them — they exist because Textual derives some of them from
    ``$accent``/``$primary``, and a hue chosen to be an accent is not always a hue
    that works as a 40-row scrollbar.
    """
    tokens = BUILTIN_THEMES[base]
    return Theme(
        name=name,
        primary=tokens.primary,
        secondary=tokens.secondary,
        warning=tokens.warning,
        error=tokens.error,
        success=tokens.success,
        accent=tokens.accent,
        foreground=tokens.foreground,
        background=tokens.background,
        surface=tokens.surface,
        panel=tokens.panel,
        boost=tokens.boost,
        dark=tokens.dark,
        luminosity_spread=tokens.luminosity_spread,
        text_alpha=tokens.text_alpha,
        ansi=tokens.ansi,
        # The base's own variables first (Catppuccin Latte sets a button
        # foreground, Gruvbox an input selection), then ours on top.
        variables={**tokens.variables, **(extra or {}), **_variables(palette)},
    )


def tau_themes() -> dict[str, Theme]:
    """The built-in τ themes, by name."""
    return _builtin_tau_themes()


# ---------------------------------------------------------------------------
# User themes
# ---------------------------------------------------------------------------


def user_theme_dir() -> Path:
    """``~/.tau/themes`` — resolved against the *live* ``config.CONFIG_PATH``.

    Imported inside the function on purpose: ``sandbox_tau_home`` rebinds
    ``config.CONFIG_PATH`` as a module attribute, and a ``from … import`` at
    module scope would capture the real path once and ignore every sandbox.
    """
    from tau_coding_agent import config as config_module

    return Path(config_module.CONFIG_PATH).parent / USER_THEME_DIRNAME


def load_user_themes(
    builtins: Mapping[str, Theme],
    directory: Path | None = None,
    *,
    errors: list[str] | None = None,
) -> dict[str, Theme]:
    """Read ``<tau dir>/themes/*.json`` into ``Theme`` objects.

    The owner's standing preference is for software that can be forked and reused
    without hard-coded paths, so a theme does not have to be merged into this file
    to exist. The format is deliberately small — a theme is a palette, and almost
    every theme is somebody else's palette with four colours changed. A whole
    theme, in ``~/.tau/themes/midnight.json``::

        {
          "extends": "mocha",
          "palette": { "bg": "#000000", "bg-alt": "#050508" },
          "textual": { "background": "#000000" }
        }

    ``extends`` names a built-in τ theme and supplies both halves (design tokens
    and starting palette); ``palette`` overrides τ colours by
    :data:`TAU_PALETTE_KEYS` name; the optional ``textual`` block overrides
    Textual's own design tokens for the widgets ``parley.tcss`` does not reach.
    The file's stem is the theme's name, so ``midnight.json`` is ``"midnight"``.

    Unreadable JSON, an unknown ``extends``, an unknown palette key and an unknown
    ``textual`` field are each an error naming the file and the offending token. A
    user theme is *the* place a silent "hmm, that colour didn't take" is most
    expensive, because the only feedback channel is the screen the user is staring
    at.

    What happens to that error is the caller's decision. With no ``errors`` list
    the first bad file raises, which is the contract a test or a non-interactive
    caller wants. Given a list, each bad file appends one message and is skipped,
    so the other themes still load — see this module's docstring for why the app
    takes the second path.
    """
    theme_dir = user_theme_dir() if directory is None else directory
    if not theme_dir.is_dir():
        return {}
    loaded: dict[str, Theme] = {}
    for path in sorted(theme_dir.glob("*.json")):
        if errors is None:
            loaded[path.stem] = _read_user_theme(path, builtins)
            continue
        try:
            loaded[path.stem] = _read_user_theme(path, builtins)
        except ThemeError as exc:
            errors.append(str(exc))
    return loaded


def _read_user_theme(path: Path, builtins: Mapping[str, Theme]) -> Theme:
    """Parse one ``<name>.json`` into a ``Theme``."""
    try:
        raw: Any = json.loads(path.read_text())
    except OSError as exc:
        raise ThemeError(f"theme {path} could not be read: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ThemeError(f"theme {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ThemeError(f"theme {path} must contain a JSON object")

    unknown_sections = sorted(set(raw) - {"extends", "palette", "textual"})
    if unknown_sections:
        raise ThemeError(
            f"theme {path} has unknown key(s) {', '.join(unknown_sections)}; "
            "a theme file holds 'extends', 'palette' and 'textual'"
        )

    base_name = raw.get("extends", DEFAULT_THEME_NAME)
    if base_name not in builtins:
        raise ThemeError(
            f"theme {path} extends {base_name!r}, which is not a built-in τ theme. "
            f"Available: {', '.join(sorted(builtins))}"
        )
    base = builtins[base_name]

    # The base's palette, with the file's overrides on top. Written back through
    # ``_variables`` so an unknown key is rejected by the same check the built-ins
    # pass, rather than becoming a ``$tau-typo`` no rule reads.
    palette = {
        key[len("tau-") :]: value for key, value in base.variables.items() if key.startswith("tau-")
    }
    overrides = raw.get("palette", {})
    if not isinstance(overrides, dict):
        raise ThemeError(f"theme {path}: 'palette' must be a JSON object")
    palette.update({str(k): str(v) for k, v in overrides.items()})

    tokens: dict[str, Any] = {
        field: getattr(base, field) for field in _TEXTUAL_THEME_FIELDS if field != "variables"
    }
    textual_overrides = raw.get("textual", {})
    if not isinstance(textual_overrides, dict):
        raise ThemeError(f"theme {path}: 'textual' must be a JSON object")
    unknown_fields = sorted(set(textual_overrides) - _TEXTUAL_THEME_FIELDS)
    if unknown_fields:
        raise ThemeError(
            f"theme {path}: 'textual' has no field(s) {', '.join(unknown_fields)}. "
            f"Valid fields: {', '.join(sorted(_TEXTUAL_THEME_FIELDS - {'variables'}))}"
        )
    tokens.update(textual_overrides)

    try:
        variables = _variables(palette)
    except ThemeError as exc:
        raise ThemeError(f"theme {path}: {exc}") from exc

    non_tau = {k: v for k, v in base.variables.items() if not k.startswith("tau-")}
    return Theme(name=path.stem, variables={**non_tau, **variables}, **tokens)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def build_theme_registry(
    directory: Path | None = None, *, errors: list[str] | None = None
) -> dict[str, Theme]:
    """Every τ theme this run can select: the built-ins plus the user's.

    A user file named after a built-in **replaces** it. That is the point of the
    directory — "I like mocha but the background is too blue" should not require
    a new name and a second selection step. A user file that *fails* does not
    replace anything, so a broken ``mocha.json`` leaves the built-in mocha
    standing rather than removing the default from the registry.

    ``errors`` is passed straight to :func:`load_user_themes`: absent, the first
    bad file raises; present, bad files are collected there and skipped here.
    """
    builtins = _builtin_tau_themes()
    return {**builtins, **load_user_themes(builtins, directory, errors=errors)}


def install_themes(
    app: Any, name: str | None = None, *, registry: Mapping[str, Theme] | None = None
) -> Theme:
    """Register every τ theme on *app* and make one of them live. Returns it.

    The whole mechanism in four lines, and the reason it is a free function
    rather than a ``Parley`` method: ``parley.tcss`` is loaded by two apps. The
    second is ``tests/test_tui_appearance._ModalHarness``, a bare ``App`` that
    hosts one modal so a tree shape can be asserted without the whole TUI — it
    needs the stylesheet, so it needs the palette the stylesheet is written
    against. Anything else that mounts τ's widgets outside ``Parley`` has the
    same need and the same one call.

    ``app.stylesheet.set_variables`` is the load-bearing half. ``App.theme``'s
    watcher schedules a ``refresh_css`` for the next idle, which is fine once the
    app is running and useless during ``__init__`` — and ``__init__`` is when the
    stylesheet is first parsed. Re-seeding the variable table here is what stands
    between ``parley.tcss`` and ``UnresolvedVariableError: $tau-bg``.
    """
    themes = build_theme_registry() if registry is None else registry
    for theme in themes.values():
        app.register_theme(theme)
    chosen = resolve_theme(name, themes)
    app.theme = chosen.name
    app.stylesheet.set_variables(app.get_css_variables())
    return chosen


def resolve_theme(name: str | None, registry: Mapping[str, Theme]) -> Theme:
    """The ``Theme`` for *name*, or the default when *name* is ``None``.

    ``None`` means "nothing configured", which is a choice τ is allowed to make.
    A *named* theme that does not exist is not: it raises, listing what there is,
    because falling back to the default here would leave a user staring at the
    wrong colours with no indication that their config key had a typo in it.
    """
    if name is None:
        name = DEFAULT_THEME_NAME
    if name not in registry:
        raise ThemeError(
            f"unknown theme {name!r}. Available: {', '.join(sorted(registry))}. "
            f"Set {THEME_CONFIG_KEY!r} in ~/.tau/config.json, or drop a "
            f"<name>.json in ~/.tau/{USER_THEME_DIRNAME}/."
        )
    return registry[name]
