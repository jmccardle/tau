"""The theme mechanism: the split, the palette contract, and the two surfaces.

Reference: docs/PLAN-0.9.4.md §6 ("Swappable TCSS themes").

The seven snapshots in ``test_tui_snapshots.py`` are the other half of this
file's job — they are what says the *default* theme still renders exactly as it
did before themes existed. What is here is everything a picture cannot assert:
that the structure/colour split is real, that every theme answers the whole
palette, that a wrong name is an error rather than a shrug, and that both
selection surfaces reach the same place.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import tau_coding_agent
from textual.theme import BUILTIN_THEMES

from tau_coding_agent import config as config_module
from tau_coding_agent.themes import (
    DEFAULT_THEME_NAME,
    TAU_PALETTE_KEYS,
    THEME_CONFIG_KEY,
    ThemeError,
    adapt_theme,
    build_theme_registry,
    load_user_themes,
    resolve_theme,
    tau_themes,
    textual_themes,
)

STYLESHEET = Path(tau_coding_agent.__file__).with_name("parley.tcss")


def _declarations(text: str) -> list[tuple[int, str, str]]:
    """``(line number, property, value)`` for every declaration in a stylesheet.

    Comments are stripped first, so the prose above a rule may still discuss a
    colour by name; only what the parser sees is scanned.
    """
    without_comments = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    found: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(without_comments.splitlines(), 1):
        for match in re.finditer(r"([\w-]+)\s*:\s*([^;{}]+)", line):
            found.append((lineno, match.group(1), match.group(2).strip()))
    return found


def _variables_used() -> set[str]:
    """Every ``$tau-…`` name the stylesheet actually *reads*, comments excluded.

    The prose in ``parley.tcss`` discusses the vocabulary (``$tau-role-*``,
    ``$tau-text-*``), so scanning the raw file would count glob patterns as
    variable names.
    """
    return {
        name
        for _, _, value in _declarations(STYLESHEET.read_text())
        for name in re.findall(r"\$tau-([\w-]+)", value)
    }


# ---------------------------------------------------------------------------
# The split is verifiable, not aspirational
# ---------------------------------------------------------------------------


def test_the_structural_stylesheet_names_no_colour() -> None:
    """No colour literal survives anywhere in ``parley.tcss``.

    This is the test that makes "a palette layered over one structural
    stylesheet" a real design rather than "a full copy per theme with extra
    steps". The failure mode it exists for is quiet: someone adds a widget, gives
    it ``color: #cdd6f4`` because that is what the rule above it used to say, and
    the light theme grows one patch of unreadable text that nobody sees until a
    user reports it. A colour that no theme can reach is a bug the moment it is
    written, so it fails here rather than on somebody's screen.

    Named CSS colours are caught alongside hex: ``border: solid red`` is exactly
    as unthemeable as ``border: solid #ff0000``. The check is deliberately over
    *declaration values only* — ``#sidebar`` is a selector, and the prose in this
    file is allowed to name a hue.
    """
    named = {
        "aqua", "black", "blue", "brown", "cyan", "fuchsia", "gold", "gray",
        "green", "grey", "indigo", "ivory", "khaki", "lime", "magenta", "maroon",
        "navy", "olive", "orange", "orchid", "pink", "plum", "purple", "red",
        "salmon", "silver", "tan", "teal", "tomato", "turquoise", "violet",
        "wheat", "white", "yellow",
    }  # fmt: skip
    offenders: list[str] = []
    for lineno, prop, value in _declarations(STYLESHEET.read_text()):
        if re.search(r"#[0-9a-fA-F]{3,8}\b", value):
            offenders.append(f"{lineno}: {prop}: {value}  (hex literal)")
        if re.search(r"\brgba?\(|\bhsla?\(", value):
            offenders.append(f"{lineno}: {prop}: {value}  (rgb()/hsl() literal)")
        for word in re.findall(r"[a-z]+", value.lower()):
            if word in named:
                offenders.append(f"{lineno}: {prop}: {value}  (named colour {word!r})")
    assert not offenders, "colour literals in the structural stylesheet:\n" + "\n".join(offenders)


def test_every_variable_the_stylesheet_names_is_in_the_palette() -> None:
    """A ``$tau-…`` the sheet uses and no theme defines is a startup crash.

    Textual raises ``UnresolvedVariableError`` on the first parse, so this failure
    is loud — but it is loud *at run time*, in a package that ships a stylesheet
    inside a wheel. Catching it here is the difference between a failed test and
    a failed install.
    """
    used = _variables_used()
    undefined = sorted(used - set(TAU_PALETTE_KEYS))
    assert not undefined, f"stylesheet names undefined variables: {undefined}"


def test_the_palette_has_no_variable_the_stylesheet_never_uses() -> None:
    """The other direction: a palette key nothing reads is 3 lines × N themes.

    Not a crash, so nothing else would ever catch it — the vocabulary would just
    accumulate dead entries that every new theme has to fill in anyway.
    """
    unused = sorted(set(TAU_PALETTE_KEYS) - _variables_used())
    assert not unused, f"palette keys no rule reads: {unused}"


# ---------------------------------------------------------------------------
# The default is load-bearing
# ---------------------------------------------------------------------------


def test_the_default_theme_is_mocha() -> None:
    assert DEFAULT_THEME_NAME == "mocha"
    assert resolve_theme(None, tau_themes()).name == "mocha"


def test_mocha_carries_textual_darks_design_tokens() -> None:
    """The default must not move Textual's OWN colours, only add τ's.

    ``textual-dark`` is ``App``'s default theme, so it is what the TUI ran under
    before themes existed: the Footer, the scrollbars, the ``Tree`` cursor line
    and the notification toasts are all coloured from these tokens and none of
    them is mentioned in ``parley.tcss``. If ``mocha`` disagreed with
    ``textual-dark`` on any of them, the seven reference SVGs would be wrong and
    the only symptom would be seven snapshot diffs with no obvious cause.

    Re-derived from ``textual.theme.BUILTIN_THEMES`` rather than asserted against
    a second copy of the numbers, so a Textual upgrade that restyles
    ``textual-dark`` fails *here*, naming the field, instead of in the snapshots.
    """
    mocha = tau_themes()["mocha"]
    upstream = BUILTIN_THEMES["textual-dark"]
    for field in (
        "primary", "secondary", "warning", "error", "success", "accent",
        "foreground", "background", "surface", "panel", "boost", "dark",
        "luminosity_spread", "text_alpha", "ansi",
    ):  # fmt: skip
        assert getattr(mocha, field) == getattr(upstream, field), field


def test_mocha_is_the_palette_the_stylesheet_used_to_hardcode() -> None:
    """Spot-check the values the pre-theme stylesheet had at those spots.

    Not exhaustive — the snapshots are exhaustive. This is the readable statement
    of the same fact, so a reader of this file can see what "byte-identical
    default" is anchored to without opening an SVG.
    """
    palette = _palette(tau_themes()["mocha"])
    assert palette["bg"] == "#1e1e2e"
    assert palette["border"] == "#45475a"
    assert palette["text"] == "#cdd6f4"
    assert palette["role-user"] == "#89dceb"
    assert palette["role-assistant"] == "#f9e2af"
    assert palette["zone-path"] == "#89b4fa"


# ---------------------------------------------------------------------------
# Every theme answers the whole palette, legibly
# ---------------------------------------------------------------------------


def _palette(theme: Any) -> dict[str, str]:
    return {k[len("tau-") :]: v for k, v in theme.variables.items() if k.startswith("tau-")}


def _is_hex_palette(theme: Any) -> bool:
    """Whether every colour in *theme* is a ``#rrggbb`` literal.

    False for ``ansi``, whose colours are ANSI *names*: the RGB behind
    ``ansi_red`` is whatever the user's terminal emulator says it is. Every test
    below that computes a contrast ratio filters on this, because measuring the
    ANSI theme would be measuring the SVG exporter's stand-in palette and then
    asserting a fact about a terminal nobody is using.
    :func:`test_the_ansi_theme_names_only_ansi_colours` is what guards that theme
    instead.
    """
    return all(value.startswith("#") for value in _palette(theme).values())


#: Theme names whose palettes can be measured. Not a hardcoded list: a fifth
#: hex theme is covered the day it is added, and a second ANSI-style theme is
#: excluded the same day, without either being remembered here.
MEASURABLE_THEMES = sorted(name for name, t in tau_themes().items() if _is_hex_palette(t))


def test_every_builtin_theme_defines_the_whole_palette() -> None:
    for name, theme in tau_themes().items():
        assert set(_palette(theme)) == set(TAU_PALETTE_KEYS), name


def test_the_ansi_theme_names_only_ansi_colours() -> None:
    """The one rule the ANSI theme has, and the one that cannot be measured.

    Its whole purpose is to defer to the 16 colours the user already curated in
    their terminal, so a single hex literal in it is a colour that ignores them —
    and it would be invisible in review, because a hex value is what every other
    theme is made of. It is also what would put this theme back into
    :data:`MEASURABLE_THEMES` and start asserting contrast ratios against the SVG
    exporter's stand-in palette.
    """
    from textual.color import Color

    palette = _palette(tau_themes()["ansi"])
    for key, value in sorted(palette.items()):
        assert value.startswith("ansi_"), f"ansi theme: {key} is {value!r}, not an ANSI name"
        assert Color.parse(value).ansi is not None, (
            f"ansi theme: {key}={value} is not an ANSI colour"
        )


def test_the_ansi_theme_paints_no_backgrounds() -> None:
    """Every surface is the terminal's own background.

    The reason is in ``themes.py``: ``ansi_black`` is a black sidebar on a light
    terminal and invisible on a dark one, so a theme that does not know which it
    is in cannot name a surface colour at all. This is the assertion that keeps
    someone from "fixing" the flat look by giving the sidebar a tint.
    """
    palette = _palette(tau_themes()["ansi"])
    for key in ("bg", "bg-alt", "bg-deep", "surface", "surface-hover"):
        assert palette[key] == "ansi_default", f"ansi theme: {key} is {palette[key]!r}"


def test_there_is_a_light_theme() -> None:
    """The one case the pre-theme TUI could not serve.

    ``dark=False`` is not cosmetic bookkeeping: it is what puts ``-light-mode`` on
    the app and sends Textual's own widgets down their light branch, so a theme
    with light τ surfaces and ``dark=True`` would be a light chat pane under a
    dark Footer.
    """
    lights = [name for name, theme in tau_themes().items() if not theme.dark]
    assert lights, "every built-in theme is dark; a light terminal is unserved"
    for name in lights:
        palette = _palette(tau_themes()[name])
        assert _luminance(palette["bg"]) > 0.5, f"{name} says dark=False but its bg is dark"


def _luminance(hex_colour: str) -> float:
    """WCAG relative luminance of ``#rrggbb``."""

    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


#: ``foreground key -> (backgrounds it is actually painted on, minimum ratio)``.
#:
#: The pairings are read off ``parley.tcss``, not assumed: ``$tau-text`` is the
#: only foreground that lands on a button (``$tau-surface``) or a hovered row
#: (``$tau-surface-hover``); ``$tau-code-fg`` lands on ``$tau-bg-deep`` and
#: nowhere else; every role hue lands on the chat pane or a dialog.
#:
#: The thresholds are tiered because the ramp is a design: ``text-faint`` is
#: *supposed* to be barely there (it is the archived-row hue), so holding it to
#: 4.5:1 would forbid the thing it is for. What each tier forbids is the failure
#: this test exists for — a hue that reads on a dark background and vanishes on a
#: light one.
_LEGIBILITY: dict[str, tuple[tuple[str, ...], float]] = {
    "text": (("bg", "bg-alt", "surface", "surface-hover"), 4.0),
    "text-soft": (("bg", "bg-alt"), 3.0),
    "text-dim": (("bg", "bg-alt"), 3.0),
    "text-quiet": (("bg", "bg-alt"), 3.0),
    "text-muted": (("bg", "bg-alt"), 2.5),
    "text-faint": (("bg", "bg-alt"), 1.6),
    "accent": (("bg", "bg-alt"), 3.0),
    "role-user": (("bg", "bg-alt"), 3.0),
    "role-assistant": (("bg", "bg-alt"), 3.0),
    "role-system": (("bg", "bg-alt"), 3.0),
    "role-tool": (("bg", "bg-alt"), 3.0),
    "role-result": (("bg", "bg-alt"), 3.0),
    "role-error": (("bg", "bg-alt"), 3.0),
    "role-blocked": (("bg", "bg-alt"), 3.0),
    "role-foreign": (("bg", "bg-alt"), 3.0),
    "role-pending": (("bg", "bg-alt"), 3.0),
    "zone-path": (("bg", "bg-alt"), 3.0),
    "code-fg": (("bg-deep",), 3.0),
}


@pytest.mark.parametrize("theme_name", MEASURABLE_THEMES)
def test_no_theme_leaves_text_unreadable_against_its_own_background(theme_name: str) -> None:
    """Dark-on-dark and light-on-light, measured rather than eyeballed.

    A theme is 25 colours and a reviewer looks at one screenshot; the hue that
    fails is the one on the surface that screenshot did not contain. Catppuccin
    Latte is the live example — its published accents are tuned as *borders* on a
    light base and several of them are barely 2:1, which is fine for a border
    glyph and not fine for ``.tool-box > CollapsibleTitle``, which paints a whole
    row of text in one.
    """
    palette = _palette(tau_themes()[theme_name])
    for foreground, (backgrounds, minimum) in _LEGIBILITY.items():
        for background in backgrounds:
            ratio = _contrast(palette[foreground], palette[background])
            assert ratio >= minimum, (
                f"{theme_name}: {foreground} ({palette[foreground]}) on "
                f"{background} ({palette[background]}) is {ratio:.2f}:1, "
                f"below the {minimum}:1 this pairing needs"
            )


def test_the_text_ramp_runs_one_way() -> None:
    """``text`` … ``text-faint`` must get monotonically quieter.

    The stylesheet uses position in the ramp to say how loud a piece of chrome is
    — ``#tree-browser-marks`` is ``text-quiet`` and ``#tree-browser-help`` one row
    below it is ``text-muted``, and that pairing is the whole reason the readout
    reads as state and the help line as instruction. A theme that puts a brighter
    colour lower in the ramp does not break; it just quietly stops meaning
    anything.
    """
    ramp = ("text", "text-soft", "text-dim", "text-quiet", "text-muted", "text-faint")
    for name in MEASURABLE_THEMES:
        palette = _palette(tau_themes()[name])
        ratios = [_contrast(palette[step], palette["bg"]) for step in ramp]
        assert ratios == sorted(ratios, reverse=True), f"{name} ramp is not monotonic: {ratios}"


# ---------------------------------------------------------------------------
# Textual's own themes, adapted
# ---------------------------------------------------------------------------
#
# The defect these cover: ``App.__init__`` registers Textual's 21 themes and its
# "Theme" system command lists every registered theme, so selecting one used to
# stop the app with ``reference to undefined variable '$tau-bg'``. τ's four
# worked; the other 21 crashed.


def _parse(value: str) -> Any:
    """``#rrggbb``, ``#rrggbb 62%`` or an ANSI name, as a ``Color``.

    ``Color.parse`` does not take the ``<colour> <percentage>`` form — that is
    handled by the CSS parser, and the derived text ramp uses it — so the
    percentage is split off and applied as alpha here.
    """
    from textual.color import Color

    head, _, tail = value.partition(" ")
    colour = Color.parse(head)
    return colour.with_alpha(float(tail.rstrip("%")) / 100) if tail else colour


def _over(value: str, background: str) -> str:
    """*value* composited onto *background*, as ``#rrggbb``.

    The alpha steps of a derived ramp are not comparable as literals: 62% of a
    foreground is a different colour on the chat pane than on a dialog, and that
    is the point of using alpha. Every measurement below flattens first.
    """
    colour, base = _parse(value), _parse(background)
    if colour.a < 1:
        colour = base.blend(colour.with_alpha(1.0), colour.a)
    return colour.hex


ADAPTED_THEMES = sorted(textual_themes())


def test_every_theme_the_registry_offers_defines_the_whole_palette() -> None:
    """Not just τ's four — the registry is what a swap can reach."""
    for name, theme in build_theme_registry().items():
        assert set(_palette(theme)) == set(TAU_PALETTE_KEYS), name


def test_adapting_a_theme_does_not_mutate_the_original() -> None:
    """``BUILTIN_THEMES`` holds one shared ``Theme`` per name for the process.

    ``Theme`` is a plain mutable dataclass, so writing the derived palette into
    ``theme.variables`` instead of a copy would give τ's palette to every other
    Textual app in the same interpreter — and, worse, make a second call see
    values it derived from itself.
    """
    before = dict(BUILTIN_THEMES["nord"].variables)
    adapted = adapt_theme(BUILTIN_THEMES["nord"])
    assert BUILTIN_THEMES["nord"].variables == before
    assert adapted.variables != before


def test_adapting_a_theme_that_already_has_a_palette_keeps_it() -> None:
    """The derivation fills what is absent; it never overrides a designed colour."""
    mocha = tau_themes()["mocha"]
    assert _palette(adapt_theme(mocha)) == _palette(mocha)


@pytest.mark.parametrize("theme_name", ADAPTED_THEMES)
def test_an_adapted_theme_reads_every_colour_off_its_own_tokens(theme_name: str) -> None:
    """No hue is invented. Every derived value is one the theme already carries.

    This is what keeps an adapted theme *that theme* rather than mocha wearing
    someone else's name — the failure would look fine in one screenshot and wrong
    in every other, because the palette would agree with the Footer nowhere.
    """
    theme = textual_themes()[theme_name]
    generated = set(theme.to_color_system().generate().values())
    generated |= {value.split(" ")[0] for value in generated}
    for key, value in sorted(_palette(theme).items()):
        assert value.split(" ")[0] in generated, (
            f"{theme_name}: {key}={value} is not a theme colour"
        )


@pytest.mark.parametrize("theme_name", ADAPTED_THEMES)
def test_an_adapted_themes_text_ramp_runs_one_way(theme_name: str) -> None:
    """The one property of the ramp the stylesheet actually depends on.

    Derived from the same argument as ``test_the_text_ramp_runs_one_way``: the
    stylesheet uses position in the ramp to say how loud a piece of chrome is. It
    is asserted separately because an adapted theme cannot be held to the *ratios*
    a designed one is — ``solarized-dark``'s brightest foreground is 4.75:1
    against its own background, so its "dim" step cannot clear 3:1 and no
    derivation can make it. Monotonic is the part that is ours to get right, and
    it is why the ramp is alpha rather than ``$foreground-darken-*`` (three of
    these themes invert with the mixed ramp).
    """
    palette = _palette(textual_themes()[theme_name])
    if not palette["bg"].startswith("#"):
        pytest.skip("ANSI palette: the RGB is the terminal's, not ours to measure")
    ramp = ("text", "text-soft", "text-dim", "text-quiet", "text-muted", "text-faint")
    ratios = [_contrast(_over(palette[step], palette["bg"]), palette["bg"]) for step in ramp]
    assert ratios == sorted(ratios, reverse=True), f"{theme_name} ramp: {ratios}"


@pytest.mark.parametrize("theme_name", ADAPTED_THEMES)
def test_an_adapted_theme_keeps_its_text_legible_on_its_own_pane(theme_name: str) -> None:
    """The floor an adapted theme is held to, and the whole floor.

    ``text`` on ``bg`` is the pairing that decides whether the app can be read at
    all, and it is the one every theme author already got right for their own
    palette — so a failure here means the derivation picked the wrong pair of
    tokens, not that the theme is dim. The role hues are deliberately *not*
    checked: τ needs ten and Textual defines six, so an adapted theme spends hues
    twice and cannot be tuned the way ``latte`` was.
    """
    palette = _palette(textual_themes()[theme_name])
    if not palette["bg"].startswith("#"):
        pytest.skip("ANSI palette: the RGB is the terminal's, not ours to measure")
    ratio = _contrast(palette["text"], palette["bg"])
    assert ratio >= 4.0, f"{theme_name}: text on bg is {ratio:.2f}:1"


def test_the_ansi_themes_reuse_taus_ansi_palette() -> None:
    """``ansi-dark`` and ``ansi-light`` have no colour ramp to derive from.

    Every surface they generate is ``transparent`` and every hue is an ANSI name,
    so the token derivation would give them invisible borders on invisible panes.
    τ already designed the palette for a terminal it cannot see; reusing it makes
    ``ansi-light`` the light-terminal ANSI theme ``themes._ANSI_PALETTE``'s note
    says you would otherwise write by hand.
    """
    expected = _palette(tau_themes()["ansi"])
    adapted = textual_themes()
    assert _palette(adapted["ansi-dark"]) == expected
    assert _palette(adapted["ansi-light"]) == expected
    assert adapted["ansi-light"].dark is False, "the light one has to say so"


# ---------------------------------------------------------------------------
# Fail Early: a name that does not exist
# ---------------------------------------------------------------------------


def test_an_unknown_theme_raises_naming_it_and_listing_the_alternatives() -> None:
    with pytest.raises(ThemeError) as excinfo:
        resolve_theme("mocah", tau_themes())
    message = str(excinfo.value)
    assert "mocah" in message
    assert "mocha" in message and "latte" in message and "gruvbox" in message


def test_a_theme_that_does_not_exist_is_not_silently_the_default() -> None:
    """The point of the rule above, stated as the behaviour it forbids."""
    with pytest.raises(ThemeError):
        resolve_theme("no-such-theme", tau_themes())


# ---------------------------------------------------------------------------
# User themes (~/.tau/themes/*.json)
# ---------------------------------------------------------------------------


def test_a_user_theme_extends_a_builtin_and_overrides_part_of_it(tmp_path: Path) -> None:
    (tmp_path / "midnight.json").write_text(
        json.dumps({"extends": "mocha", "palette": {"bg": "#000000"}})
    )
    themes = load_user_themes(tau_themes(), tmp_path)
    palette = _palette(themes["midnight"])
    assert palette["bg"] == "#000000"
    # Everything not named is inherited, so a two-line file is a whole theme.
    assert palette["text"] == _palette(tau_themes()["mocha"])["text"]
    assert set(palette) == set(TAU_PALETTE_KEYS)


def test_a_user_theme_may_override_textual_design_tokens(tmp_path: Path) -> None:
    """The half ``parley.tcss`` cannot reach — the Footer, the scrollbars."""
    (tmp_path / "midnight.json").write_text(
        json.dumps({"extends": "mocha", "textual": {"background": "#000000"}})
    )
    assert load_user_themes(tau_themes(), tmp_path)["midnight"].background == "#000000"


def test_a_user_theme_replaces_a_builtin_of_the_same_name(tmp_path: Path) -> None:
    """ "I like mocha but the background is too blue" should not need a new name."""
    (tmp_path / "mocha.json").write_text(json.dumps({"palette": {"bg": "#000000"}}))
    registry = build_theme_registry(tmp_path)
    assert _palette(registry["mocha"])["bg"] == "#000000"
    assert set(registry) == set(build_theme_registry()), (
        "replacing a built-in must not add or drop a name"
    )


def test_a_theme_file_that_will_not_parse_says_which_file(tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text("{ not json")
    with pytest.raises(ThemeError) as excinfo:
        load_user_themes(tau_themes(), tmp_path)
    assert "broken.json" in str(excinfo.value)


def test_a_theme_file_naming_a_palette_key_that_does_not_exist_says_which(
    tmp_path: Path,
) -> None:
    """A typo'd key is a colour the user set and never got — the worst silence."""
    (tmp_path / "typo.json").write_text(json.dumps({"palette": {"backgruond": "#000"}}))
    with pytest.raises(ThemeError) as excinfo:
        load_user_themes(tau_themes(), tmp_path)
    assert "backgruond" in str(excinfo.value)
    assert "typo.json" in str(excinfo.value)


def test_a_theme_file_extending_something_that_is_not_a_theme_says_so(tmp_path: Path) -> None:
    (tmp_path / "x.json").write_text(json.dumps({"extends": "solarized"}))
    with pytest.raises(ThemeError) as excinfo:
        load_user_themes(tau_themes(), tmp_path)
    assert "solarized" in str(excinfo.value)


def test_no_themes_directory_is_not_an_error(tmp_path: Path) -> None:
    """Nobody has one. Absence is a choice τ is allowed to make for the user."""
    assert load_user_themes(tau_themes(), tmp_path / "nope") == {}


def test_the_user_theme_dir_follows_the_sandboxed_config_path(tau_home: Path) -> None:
    """A "hermetic" test must not read the developer's real ``~/.tau/themes``.

    ``sandbox_tau_home`` redirects ``config.CONFIG_PATH`` and nothing else, so
    resolving the theme directory from ``config.TAU_DIR`` — which is frozen at
    import — is exactly the leak ``tests/conftest.py``'s docstring is about.
    """
    from tau_coding_agent.themes import user_theme_dir

    assert config_module.CONFIG_PATH.parent == tau_home
    assert user_theme_dir() == tau_home / "themes"


# ---------------------------------------------------------------------------
# The two selection surfaces
# ---------------------------------------------------------------------------


def _write_config(tau_home: Path, **keys: Any) -> None:
    """Seed ``<sandbox>/config.json`` before the app is constructed.

    ``build_parley`` assigns ``app.config`` *after* ``Parley.__init__`` has run,
    and the theme is resolved inside ``__init__`` (it has to be — the stylesheet
    is parsed there). So a test about the *config* surface has to put the key on
    disk, which is also what a real user does.
    """
    from tau_coding_agent.testing.sandbox import DEFAULT_CONFIG

    (tau_home / "config.json").write_text(json.dumps({**DEFAULT_CONFIG, **keys}))


def _screen_background(app: Any) -> str:
    """The colour ``Screen { background: $tau-bg; }`` resolved to, as ``#rrggbb``."""
    return app.screen.styles.background.hex.lower()


async def test_the_default_is_unchanged_when_nothing_is_configured(make_app) -> None:
    """A config with no ``theme`` key gets Mocha, and Mocha is the old look."""
    app = make_app()
    async with app.run_test():
        assert app.theme == "mocha"
        assert _screen_background(app) == "#1e1e2e"


async def test_switching_themes_at_runtime_changes_the_applied_styles(make_app) -> None:
    """The in-session half of "selectable / swappable" — no restart.

    Asserted on *resolved widget styles*, not on ``app.theme``: setting the
    reactive is easy and proves nothing, and the failure this guards against is a
    theme that registers cleanly and never reaches the stylesheet (which is
    exactly what happens if ``set_variables``/``refresh_css`` is skipped).
    Two widgets, because ``Screen`` is styled by a type selector and
    ``#chat-input`` by an id — a variable table that only half-refreshed would
    show up as one of them moving.
    """
    app = make_app()
    async with app.run_test() as pilot:
        # ``on_mount`` focuses the input, so this is the ``#chat-input:focus``
        # rule — ``border: solid $tau-role-user``, the one place a role hue is
        # visible on the first frame.
        chat_input = app.query_one("#chat-input")
        assert _screen_background(app) == "#1e1e2e"
        assert chat_input.styles.border_top[1].hex.lower() == "#89dceb"

        app.action_set_theme("latte")
        await pilot.pause()
        await pilot.pause()

        assert app.theme == "latte"
        assert _screen_background(app) == "#eff1f5"
        assert chat_input.styles.border_top[1].hex.lower() == "#0e6f8a"


async def test_a_runtime_swap_moves_textuals_own_widgets_too(make_app) -> None:
    """Not just ``$tau-*``: the Footer and the scrollbars follow the design tokens.

    ``-light-mode`` on the app is the flag Textual's built-in CSS branches on, so
    this is the assertion that a light τ palette does not end up under a dark
    Footer — the failure mode of shipping a bare variables dict instead of a
    ``textual.theme.Theme``.
    """
    app = make_app()
    async with app.run_test() as pilot:
        assert app.has_class("-dark-mode")
        app.action_set_theme("latte")
        await pilot.pause()
        assert app.has_class("-light-mode")
        assert not app.has_class("-dark-mode")


async def test_the_config_key_selects_the_theme_at_startup(make_app, tau_home: Path) -> None:
    _write_config(tau_home, theme="gruvbox")
    app = make_app()
    async with app.run_test():
        assert app.theme == "gruvbox"
        assert _screen_background(app) == "#282828"


async def test_a_runtime_swap_round_trips_through_the_config_file(make_app, tau_home: Path) -> None:
    """The two surfaces are one setting, not two.

    A theme chosen from the palette is written back through the same
    ``update_config`` read-modify-write ``action_edit_system_prompt`` uses, so the
    next launch opens in it — and the ``models``/``system_prompt`` already on disk
    survive the write.
    """
    _write_config(tau_home, theme="mocha")
    app = make_app()
    async with app.run_test():
        app.action_set_theme("latte")

    on_disk = json.loads((tau_home / "config.json").read_text())
    assert on_disk[THEME_CONFIG_KEY] == "latte"
    assert on_disk["default_model"] == "m", "the theme write clobbered the rest of the config"

    reopened = make_app()
    async with reopened.run_test():
        assert reopened.theme == "latte"


# ---------------------------------------------------------------------------
# A theme that fails: an error toast and the default, not a dead terminal
# ---------------------------------------------------------------------------
#
# The decision (2026-08-24) is that no theme problem stops τ from starting. The
# module docstring in ``themes.py`` has the reasoning; what these assert is the
# pair of things that has to be true for it to be Fail Early rather than a silent
# fallback: the app runs, AND the user is told why the colours are not theirs.


def _toasts(app: Any) -> list[str]:
    """Every notification currently raised, as text.

    Read off ``App._notifications`` rather than off the rendered ``ToastRack``: a
    toast's widget is mounted on the next refresh, so asserting on the screen
    makes these tests a race with the compositor, while the notification itself
    exists the moment ``notify`` is called.
    """
    return [notification.message for notification in app._notifications]


async def test_a_configured_theme_that_does_not_exist_toasts_and_uses_the_default(
    make_app, tau_home: Path
) -> None:
    """A typo in config.json costs you the colours, not the session."""
    _write_config(tau_home, theme="mocah")
    app = make_app()
    async with app.run_test():
        assert app.theme == DEFAULT_THEME_NAME
        message = "\n".join(_toasts(app))
        assert "mocah" in message, "the toast must name what the user actually typed"
        assert "mocha" in message, "and what they could have meant"


async def test_a_theme_key_that_is_not_a_name_toasts_and_uses_the_default(
    make_app, tau_home: Path
) -> None:
    _write_config(tau_home, theme=["mocha"])
    app = make_app()
    async with app.run_test():
        assert app.theme == DEFAULT_THEME_NAME
        assert THEME_CONFIG_KEY in "\n".join(_toasts(app))


async def test_one_broken_theme_file_does_not_stop_tau_from_starting(
    make_app, tau_home: Path
) -> None:
    """The case that drove the decision.

    A file the user is not even selecting used to take the whole TUI down, which
    is Fail Early manufacturing a problem rather than exposing one. The other
    themes still load, the built-in default is still live, and the broken file is
    named in a toast.
    """
    themes_dir = tau_home / "themes"
    themes_dir.mkdir()
    (themes_dir / "broken.json").write_text("{ not json")
    (themes_dir / "midnight.json").write_text(json.dumps({"palette": {"bg": "#000000"}}))

    app = make_app()
    async with app.run_test():
        assert app.theme == DEFAULT_THEME_NAME
        assert "midnight" in app._theme_registry, "one bad file must not cost the good ones"
        assert "broken" not in app._theme_registry
        assert "broken.json" in "\n".join(_toasts(app))


async def test_a_broken_file_named_after_a_builtin_leaves_the_builtin_standing(
    make_app, tau_home: Path
) -> None:
    """The worst case: the broken file shadows the theme we would fall back to.

    A registry that dropped ``mocha`` here would make the fallback itself
    unresolvable, so the app has to keep the built-in when its replacement fails.
    """
    themes_dir = tau_home / "themes"
    themes_dir.mkdir()
    (themes_dir / "mocha.json").write_text("{ not json")

    app = make_app()
    async with app.run_test():
        assert app.theme == "mocha"
        assert _screen_background(app) == "#1e1e2e", "the built-in mocha, not a half-built one"


async def test_a_runtime_swap_to_an_unknown_theme_reports_and_keeps_the_current_one(
    make_app,
) -> None:
    """The startup rule is wrong for a swap.

    Falling back to the default here would take the colours away from a user who
    was already running a theme they chose, because they mistyped a different one.
    """
    app = make_app()
    async with app.run_test() as pilot:
        app.action_set_theme("gruvbox")
        await pilot.pause()
        app.action_set_theme("gruvbocks")
        await pilot.pause()
        assert app.theme == "gruvbox"
        assert "gruvbocks" in "\n".join(_toasts(app))


async def test_a_clean_start_raises_no_theme_toast(make_app) -> None:
    """The other half of the rule above: silence when nothing is wrong.

    A notice that appears on every launch is a notice nobody reads, which would
    cost the failing case the only channel it has.
    """
    app = make_app()
    async with app.run_test():
        assert not app._theme_errors
        assert not [message for message in _toasts(app) if "theme" in message.lower()]


# ---------------------------------------------------------------------------
# The three selection surfaces
# ---------------------------------------------------------------------------


async def test_the_theme_flag_selects_the_theme_for_this_run(make_app, tau_home: Path) -> None:
    """``--theme`` beats the config key, which is what an override is."""
    _write_config(tau_home, theme="mocha")
    app = make_app(cli_overrides={"theme": "gruvbox"})
    async with app.run_test():
        assert app.theme == "gruvbox"
        assert _screen_background(app) == "#282828"


async def test_the_theme_flag_does_not_reach_the_config_file(make_app, tau_home: Path) -> None:
    """The whole difference between the flag and the palette.

    Picking from the palette saves, because there is nowhere else to put the
    choice. A flag is this invocation only, and a flag that quietly rewrote the
    config would make ``--theme`` impossible to use for "let me just try one".
    """
    _write_config(tau_home, theme="mocha")
    app = make_app(cli_overrides={"theme": "gruvbox"})
    async with app.run_test():
        assert app.theme == "gruvbox"
    assert json.loads((tau_home / "config.json").read_text())[THEME_CONFIG_KEY] == "mocha"


async def test_a_swap_inside_a_theme_flag_session_saves_what_was_picked(
    make_app, tau_home: Path
) -> None:
    """The trap the in-memory override has to avoid.

    ``--theme gruvbox`` writes gruvbox into ``self.config``. If ``action_set_theme``
    saved ``self.config`` back, picking latte from the palette would save gruvbox
    too — the flag riding into the file on the back of an unrelated choice.
    ``update_config`` re-reading the file is what makes that impossible.
    """
    _write_config(tau_home, theme="mocha")
    app = make_app(cli_overrides={"theme": "gruvbox"})
    async with app.run_test():
        app.action_set_theme("latte")

    assert json.loads((tau_home / "config.json").read_text())[THEME_CONFIG_KEY] == "latte"


async def test_an_unknown_theme_flag_toasts_and_uses_the_default(make_app) -> None:
    """The flag is not validated in ``cli.py``, so this is where a typo lands."""
    app = make_app(cli_overrides={"theme": "gruvbocks"})
    async with app.run_test():
        assert app.theme == DEFAULT_THEME_NAME
        assert "gruvbocks" in "\n".join(_toasts(app))


async def test_the_command_palette_offers_every_theme_and_marks_the_active_one(
    make_app,
) -> None:
    """The palette is the discovery surface; a list that hides the current entry
    makes "which one am I on" unanswerable from the only screen that lists them."""
    app = make_app()
    async with app.run_test():
        titles = [command.title for command in app.get_system_commands(app.screen)]
        theme_entries = [title for title in titles if title.startswith("Theme: ")]
        expected = [
            f"Theme: {name}" + (" (active)" if name == DEFAULT_THEME_NAME else "")
            for name in sorted(build_theme_registry())
        ]
        assert theme_entries == expected
        # Textual's own themes are in there, adapted — the list is the registry
        # and the registry is what a swap can reach.
        assert "Theme: nord" in theme_entries


@pytest.mark.parametrize("theme_name", sorted(build_theme_registry()))
async def test_every_theme_the_app_offers_applies_to_the_real_stylesheet(
    make_app, theme_name: str
) -> None:
    """The regression test for ``reference to undefined variable '$tau-bg'``.

    Registering a theme and *applying* it are different failures: a theme with a
    hole in its palette registers cleanly and then stops the app when
    ``parley.tcss`` is re-parsed against it. So this drives the real swap on the
    real stylesheet, once per theme, and reads a colour back off a widget.
    """
    app = make_app()
    async with app.run_test():
        app.action_set_theme(theme_name)
        assert app.theme == theme_name
        assert _screen_background(app).startswith("#")


async def test_textuals_own_theme_palette_reaches_the_same_themes(make_app) -> None:
    """Textual's "Theme" system command assigns ``app.theme`` directly.

    It never goes through ``action_set_theme``, so the palette τ registers is the
    only thing standing between that command and the crash. Setting the reactive
    is exactly what ``ThemeProvider`` does.
    """
    app = make_app()
    async with app.run_test() as pilot:
        app.theme = "solarized-light"
        await pilot.pause()
        assert _screen_background(app) == "#fdf6e3"


async def test_a_theme_set_the_textual_way_is_remembered(make_app, tau_home: Path) -> None:
    """Two theme lists in one palette, one of which forgot the choice at the next
    launch, would be worse than either behaviour on its own."""
    app = make_app()
    async with app.run_test() as pilot:
        app.theme = "nord"
        await pilot.pause()
    assert json.loads((tau_home / "config.json").read_text())[THEME_CONFIG_KEY] == "nord"
