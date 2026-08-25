"""The empty chat pane, and the ``--fun`` flag that picks its tagline.

Closes handoff §4.4 ("the empty chat area says nothing"). Two things are under
test and they are separable:

1. **The pane states configuration, and states it truthfully.** It exists because
   nothing else on that screen says which model the next turn will reach. A pane
   that said the wrong model would be worse than the blank column it replaced, so
   the tool row is asserted against the SAME resolver ``TauBackend`` constructs
   from, and an unknown ``default_model`` is asserted to be reported rather than
   hidden.

2. **The pane is visible exactly while the display holds no messages.**
   :meth:`ChatDisplay._sync_placeholder` derives that from the DOM, so it cannot
   go stale; the only failure mode left is a code path that forgets to call it,
   which is why every public entry point gets its own case below.

The ``--fun`` cases pin the containment promise: the flag reaches one string and
stops. They also pin the packaging invariant that replaced a build-time rewrite:
``FUN_DEFAULT`` is ``True`` in the source, identically in a checkout and in every
built artifact, and the deterministic surfaces stay deterministic by *asking* for
``fun=False`` rather than inheriting whatever the default happens to be. The old
arrangement — ``False`` in source, ``sed``-flipped to ``True`` by ``package.sh``
— shipped the developer default in every PyPI wheel, because ``publish.yml``
builds those without ever running ``package.sh``.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from tau_coding_agent.app import ChatDisplay, ChatPlaceholder, MessageBox, Parley
from tau_coding_agent.backends import DEFAULT_TOOL_NAMES, resolve_tool_names
from tau_coding_agent.tagline import FUN_DEFAULT, TAGLINES, pick_tagline

# --- helpers ----------------------------------------------------------------


def _pane_text(app: Parley) -> str:
    """The placeholder's rendered text, as one string."""
    placeholder = app.query_one(ChatPlaceholder)
    return placeholder.render().plain  # type: ignore[union-attr]


def _visible(app: Parley) -> bool:
    return bool(app.query_one(ChatPlaceholder).display)


# --- what the pane says -----------------------------------------------------


async def test_pane_states_the_model_and_endpoint_the_next_turn_will_use(make_app):
    """The one fact no other widget on that screen carries."""
    app = make_app(
        config={
            "models": {"local": {"backend": "openai", "model": "m-42", "base_url": "http://h/v1"}},
            "default_model": "local",
        }
    )
    async with app.run_test():
        text = _pane_text(app)
        assert "m-42" in text
        assert "http://h/v1" in text


async def test_endpoint_names_the_backend_when_an_entry_has_no_base_url(make_app):
    """A first-party entry (anthropic/gemini) addresses its own endpoint.

    Naming the backend is the true answer there. A blank row would read as
    "nowhere", which is a different and false claim.
    """
    app = make_app(
        config={
            "models": {"c": {"backend": "anthropic", "model": "claude-x"}},
            "default_model": "c",
        }
    )
    async with app.run_test():
        assert "anthropic" in _pane_text(app)


async def test_pane_reports_an_unknown_default_model_instead_of_hiding_it(make_app):
    """Fail-Early: this is the same condition ``action_new_chat`` refuses to start on.

    The pane is where a user can see it BEFORE typing a prompt and waiting. A
    pane that fell back to printing the bare name would look like a working
    config.
    """
    app = make_app(config={"models": {"a": {"backend": "openai"}}, "default_model": "ghost"})
    async with app.run_test():
        text = _pane_text(app)
        assert "ghost" in text
        assert "not in config.json" in text


async def test_tools_row_is_what_the_backend_would_construct(make_app):
    """Asserted through the shared resolver, not against a hand-written list.

    ``resolve_tool_names`` is the single reader of ``tools``/``exclude_tools``;
    if this row ever stopped agreeing with it, the pane would be advertising a
    tool the next turn does not have.
    """
    app = make_app()
    async with app.run_test():
        entry = app.config["models"][app.config["default_model"]]
        expected = resolve_tool_names(app._apply_run_config(entry))
        assert expected == list(DEFAULT_TOOL_NAMES)
        assert " ".join(expected) in _pane_text(app)


async def test_exclude_tools_is_reflected_in_the_pane(make_app):
    """``--exclude-tools bash`` has to remove bash from the row, not just the run."""
    app = make_app(cli_run_config={"exclude_tools": ["bash"]})
    async with app.run_test():
        text = _pane_text(app)
        assert "read" in text
        assert "bash" not in text


async def test_no_builtin_tools_says_so_rather_than_leaving_a_blank_row(make_app):
    """An empty tool set is a fact worth reading, so it is spelled out."""
    app = make_app(cli_run_config={"no_tools": "builtin"})
    async with app.run_test():
        assert "none (--no-builtin-tools)" in _pane_text(app)


async def test_no_tools_row_names_the_flag_that_actually_emptied_it(make_app):
    """The row must distinguish the two flags: under ``--no-tools`` the next turn
    has NOTHING, while under ``--no-builtin-tools`` an extension may still be
    offering tools this row (built-ins only) does not list. Naming the wrong flag
    is the difference between those two claims."""
    app = make_app(cli_run_config={"no_tools": "all"})
    async with app.run_test():
        text = _pane_text(app)
        assert "none (--no-tools)" in text
        assert "--no-builtin-tools" not in text


async def test_config_declared_empty_tool_list_is_not_blamed_on_a_flag(make_app):
    """A ``"tools": []`` in config.json is an empty set no flag caused, so the row
    says a plain ``none`` rather than attributing it to one."""
    app = make_app(
        config={
            "models": {"m": {"backend": "openai", "model": "x", "tools": []}},
            "default_model": "m",
        }
    )
    async with app.run_test():
        text = _pane_text(app)
        assert "none" in text
        assert "--no-tools" not in text


async def test_cwd_row_collapses_home_to_a_tilde(make_app):
    app = make_app()
    app._cwd = Path.home() / "Development" / "thing"
    async with app.run_test():
        assert "~/Development/thing" in _pane_text(app)


async def test_cwd_outside_home_is_shown_unchanged(make_app):
    app = make_app()
    app._cwd = Path("/srv/checkout")
    async with app.run_test():
        assert "/srv/checkout" in _pane_text(app)


async def test_pane_carries_no_reachability_claim(make_app):
    """No probe runs, so no probe result may be implied.

    Guards the deliberate decision NOT to show a connection tick: the pane states
    what τ was configured with, never what it has reached.
    """
    app = make_app()
    async with app.run_test():
        text = _pane_text(app).lower()
        for claim in ("connected", "online", "reachable", "✓"):
            assert claim not in text


async def test_pane_explains_the_tree_key_rather_than_repeating_the_footer(make_app):
    """The hint's whole justification: ``^g Tree`` names a key without saying what it does."""
    app = make_app()
    async with app.run_test():
        assert "branch from any earlier message" in _pane_text(app)


# --- when the pane is visible -----------------------------------------------


async def test_pane_is_visible_on_a_fresh_app(make_app):
    app = make_app()
    async with app.run_test():
        assert _visible(app)


async def test_pane_hides_when_a_message_is_added(make_app):
    app = make_app()
    async with app.run_test() as pilot:
        display = app.query_one(ChatDisplay)
        display.add_message("user", "hello", source="verbatim")
        await pilot.pause()
        assert not _visible(app)


async def test_pane_hides_when_a_persisted_message_is_rendered(make_app):
    app = make_app()
    async with app.run_test() as pilot:
        display = app.query_one(ChatDisplay)
        display.add_persisted_message({"role": "user", "content": "hi"})
        await pilot.pause()
        assert not _visible(app)


async def test_pane_hides_when_an_exchange_opens(make_app):
    """The streaming path mounts an ExchangeBox, not a MessageBox."""
    app = make_app()
    async with app.run_test() as pilot:
        display = app.query_one(ChatDisplay)
        await display.begin_exchange()
        await pilot.pause()
        assert not _visible(app)


async def test_pane_returns_after_the_display_is_cleared(make_app):
    """``ctrl+n`` / clear-chat land here. The pane coming back IS the empty state."""
    app = make_app()
    async with app.run_test() as pilot:
        display = app.query_one(ChatDisplay)
        display.add_message("user", "hello", source="verbatim")
        await pilot.pause()
        assert not _visible(app)

        await display.clear_messages()
        await pilot.pause()
        assert _visible(app)
        assert not app.query(MessageBox)


async def test_facts_are_re_read_when_the_pane_reappears(make_app):
    """The model can change (/model, a resumed session) while the chat is empty.

    A pane that snapshotted its facts at construction would keep stating the old
    model after the swap — a visible fact that is quietly false.
    """
    app = make_app(
        config={
            "models": {
                "one": {"backend": "openai", "model": "first"},
                "two": {"backend": "openai", "model": "second"},
            },
            "default_model": "one",
        }
    )
    async with app.run_test() as pilot:
        assert "first" in _pane_text(app)

        display = app.query_one(ChatDisplay)
        display.add_message("user", "hello", source="verbatim")
        await pilot.pause()
        app.config["default_model"] = "two"
        await display.clear_messages()
        await pilot.pause()

        assert "second" in _pane_text(app)
        assert "first" not in _pane_text(app)


async def test_a_display_built_without_facts_composes_no_pane():
    """Two bare renderer harnesses construct ``ChatDisplay()`` with no app around it.

    ``None`` composes nothing rather than inventing facts nobody supplied.
    """
    from textual.app import App, ComposeResult

    class _Bare(App):
        def compose(self) -> ComposeResult:
            yield ChatDisplay()

    async with _Bare().run_test() as pilot:
        assert not pilot.app.query(ChatPlaceholder)


# --- --fun ------------------------------------------------------------------


def test_fun_off_always_returns_the_first_tagline():
    """The whole deterministic-render contract in one line."""
    assert all(pick_tagline(False) == TAGLINES[0] for _ in range(50))


def test_fun_on_eventually_picks_something_other_than_the_first():
    random.seed(0)
    picks = {pick_tagline(True) for _ in range(200)}
    assert picks - {TAGLINES[0]}
    assert picks <= set(TAGLINES)


def test_fun_default_is_on_in_the_source_tree():
    """The value a packaged user gets, asserted where it is actually written.

    This is the whole fix for "``--fun`` never reaches anyone who installed τ":
    the default lives in the source, so every artifact carries it — the GitHub
    tarball from ``package.sh``, the five PyPI wheels from ``python -m build``,
    and an editable checkout alike.
    """
    assert FUN_DEFAULT is True

    source = Path(__file__).resolve().parents[1] / "src" / "tau_coding_agent" / "tagline.py"
    assert "\nFUN_DEFAULT = True\n" in source.read_text()


def test_no_build_path_rewrites_the_fun_default():
    """No build step may patch this default in — that is how it got lost before.

    ``package.sh`` used to ``sed`` ``FUN_DEFAULT`` on in its staged copy, which
    left the PyPI wheels (built by ``publish.yml``, straight from source) with
    the developer default. Any rewrite reintroduced in either build path has the
    same shape, so neither file may mention the name at all.
    """
    root = Path(__file__).resolve().parents[2]
    for build_file in (root / "package.sh", root / ".github" / "workflows" / "publish.yml"):
        assert build_file.exists(), build_file
        for line in build_file.read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue  # the comments explain why the rewrite is gone
            assert "FUN_DEFAULT" not in line, f"{build_file.name}: {line}"


def test_deterministic_surfaces_name_their_own_fun_rather_than_inheriting_it():
    """``Parley``'s default is the literal ``False``, not :data:`FUN_DEFAULT`.

    The snapshot suite, ``testing.scenes`` and ``devshot`` all construct a
    ``Parley`` without saying ``fun``, so this signature IS their determinism.
    Wiring it to ``FUN_DEFAULT`` would make every rendered scene a coin flip.
    """
    import inspect

    default = inspect.signature(Parley.__init__).parameters["fun"].default
    assert default is False


async def test_parley_defaults_to_the_deterministic_tagline_regardless_of_packaging(make_app):
    """``Parley()`` takes ``fun=False``, NOT ``tagline.FUN_DEFAULT``.

    Only ``cli.py`` passes the packaged default through, so a test or a scene
    built in a packaged tree renders the same as one built here.
    """
    app = make_app()
    async with app.run_test():
        assert TAGLINES[0] in _pane_text(app)


async def test_fun_true_reaches_the_pane_and_nothing_else(tau_home, monkeypatch):
    """The flag's entire observable effect: one string on one widget."""
    from tau_coding_agent.testing.sandbox import build_parley

    monkeypatch.setattr(random, "choice", lambda seq: seq[3])
    app = build_parley(tau_home, fun=True)
    async with app.run_test():
        assert TAGLINES[3] in _pane_text(app)
        # Nothing downstream can branch on it — the app keeps a string, not a flag.
        assert not hasattr(app, "fun")
        assert isinstance(app._tagline, str)


def test_cli_default_is_the_packaged_default_and_it_is_on():
    """``tau`` with no flag is fun, and cli.py is the only reader of that value."""
    from tau_coding_agent.cli import parse_cli_args

    assert parse_cli_args([]).fun is FUN_DEFAULT
    assert parse_cli_args([]).fun is True


def test_cli_can_turn_fun_on_and_off_explicitly():
    """Both directions are expressible, which is why this is the flag set's one
    paired boolean: it defaults ON, so ``--no-fun`` is how anyone — a user, or a
    developer reproducing a scene — gets a fixed screen back."""
    from tau_coding_agent.cli import parse_cli_args

    assert parse_cli_args(["--fun"]).fun is True
    assert parse_cli_args(["--no-fun"]).fun is False


def test_tagline_list_has_no_duplicates():
    assert len(set(TAGLINES)) == len(TAGLINES)


@pytest.mark.parametrize("tagline", TAGLINES)
def test_every_tagline_fits_the_narrowest_chat_column(tagline: str):
    """50 columns is the measured content width of the chat at 80x24 with the
    sidebar open (``ChatDisplay`` is 54 wide there, less 4 of padding). A tagline
    that wrapped would break the pane's one-line identity row."""
    assert len(tagline) <= 50
