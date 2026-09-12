"""Every extension command has a name nothing can take from it.

Reference: docs/EXTENSION-NAMESPACE.md. §1 of that doc is the table these tests
pin: with two extensions registering the same typed name, disabling either one
used to destroy a command belonging to the other, and a reload silently stole the
name back. The private registry makes those structural rather than guarded, so
each row here is a behaviour with no code branch behind it.
"""

from __future__ import annotations

import pytest

from tau_agent_core.agent_session import AgentSession
from tau_agent_core.capabilities import Argument
from tau_agent_core.commands import (
    extension_owner,
    is_qualified_command,
    qualified_command,
    resolve_command,
    split_qualified_command,
)
from tau_agent_core.session_log import InMemorySessionLog
from tau_llm.types import Model

_A = """
def register(api):
    async def note(args, ctx):
        return f"A:{args}"
    api.register_command("note", {"description": "A's note", "handler": note})
"""

_B = """
def register(api):
    async def note(args, ctx):
        return f"B:{args}"
    api.register_command("note", {"description": "B's note", "handler": note})
"""

_WRAPPER = """
def register(api):
    async def note(args, ctx):
        prior = await api.run_command("ext:a_ext.note", args)
        return f"wrapped[{prior}]"
    api.register_command("note", {"description": "wraps A", "handler": note})
"""


def _model() -> Model:
    return Model(
        id="gpt-4o",
        name="GPT-4o",
        api="openai-completions",
        provider="openai",
        base_url="https://api.openai.com/v1",
        context_window=128000,
        max_tokens=4096,
    )


def _session() -> AgentSession:
    return AgentSession(session_log=InMemorySessionLog(), model=_model())


async def _load(session: AgentSession, tmp_path, **files: str) -> dict[str, str]:
    """Write each ``name=source`` as ``<name>.py`` and load them in argument order."""
    paths = {}
    for stem, source in files.items():
        path = tmp_path / f"{stem}.py"
        path.write_text(source)
        paths[stem] = str(path)
    await session.load_extensions(list(paths.values()), discover=False)
    return paths


class TestTheGrammar:
    def test_a_qualified_name_round_trips(self):
        assert qualified_command("pirate", "speak") == "ext:pirate.speak"
        assert split_qualified_command("ext:pirate.speak") == ("pirate", "speak")
        assert is_qualified_command("ext:pirate.speak")
        assert not is_qualified_command("speak")

    def test_a_dotted_owner_still_splits(self):
        """The split takes the LAST dot, so a stem like ``my.ext`` survives."""
        assert split_qualified_command("ext:my_ext.speak") == ("my_ext", "speak")

    def test_a_dotted_command_name_is_refused(self):
        """Fail-Early: τ controls this half, so it is the half that must stay clean."""
        with pytest.raises(ValueError, match="separator"):
            qualified_command("pirate", "say.it")

    def test_an_owner_is_normalized_from_its_label(self):
        assert extension_owner("/home/j/.tau/extensions/pirate.py") == "pirate"
        assert extension_owner("/home/j/.tau/extensions/my.ext.py") == "my_ext"
        assert extension_owner("test_mod:TestThing.test_x.<locals>.my_ext") == "my_ext"

    def test_a_label_with_nothing_usable_raises(self):
        with pytest.raises(ValueError, match="no usable name"):
            extension_owner("...")


class TestTheContestedNameGoesToWhoeverAsksFirst:
    async def test_first_wins_and_the_loser_is_told_who_holds_it(self, tmp_path):
        session = _session()
        await _load(session, tmp_path, a_ext=_A, b_ext=_B)

        assert session._registry.get_bindings() == {"note": "ext:a_ext.note"}
        assert sorted(session._registry.get_commands()) == ["ext:a_ext.note", "ext:b_ext.note"]
        assert (await session.run_extension_command("note", "x")).output == "A:x"

    async def test_the_loser_is_still_reachable_by_its_own_name(self, tmp_path):
        session = _session()
        await _load(session, tmp_path, a_ext=_A, b_ext=_B)

        assert (await session.run_extension_command("ext:b_ext.note", "x")).output == "B:x"
        assert resolve_command("/ext:b_ext.note x", session._registry.command_names()) is not None

    async def test_load_order_decides_and_nothing_else_does(self, tmp_path):
        session = _session()
        await _load(session, tmp_path, b_ext=_B, a_ext=_A)

        assert session._registry.get_bindings() == {"note": "ext:b_ext.note"}


class TestDisableAndReloadStopBeingDestructive:
    """docs/EXTENSION-NAMESPACE.md §1 — the four rows, corrected."""

    async def test_disabling_the_shadowed_extension_leaves_the_winner_alone(self, tmp_path):
        session = _session()
        paths = await _load(session, tmp_path, a_ext=_A, b_ext=_B)
        # A holds the typed name here, so B is the shadowed one.
        assert (await session.disable_extension(paths["b_ext"])).ok

        assert (await session.run_extension_command("note", "x")).output == "A:x"
        assert "ext:b_ext.note" not in session._registry.get_commands()

    async def test_disabling_the_holder_unbinds_the_typed_name(self, tmp_path):
        """No stack: the typed name goes unbound rather than falling to whoever
        asked second. That extension is at its own name and a pin can move it."""
        session = _session()
        paths = await _load(session, tmp_path, a_ext=_A, b_ext=_B)
        assert (await session.disable_extension(paths["a_ext"])).ok

        assert session._registry.get_bindings() == {}
        assert (await session.run_extension_command("note", "x")).handled is False
        assert (await session.run_extension_command("ext:b_ext.note", "x")).output == "B:x"

    async def test_reloading_the_shadowed_extension_does_not_steal_the_name(self, tmp_path):
        session = _session()
        paths = await _load(session, tmp_path, a_ext=_A, b_ext=_B)
        assert (await session.reload_extension(paths["b_ext"])).ok

        assert session._registry.get_bindings() == {"note": "ext:a_ext.note"}
        assert (await session.run_extension_command("note", "x")).output == "A:x"


class TestCompositionIsByNameAndLateBound:
    async def test_a_wrapper_calls_what_it_could_not_claim(self, tmp_path):
        session = _session()
        await _load(session, tmp_path, a_ext=_A, w_ext=_WRAPPER)

        assert (await session.run_extension_command("ext:w_ext.note", "x")).output == (
            "wrapped[A:x]"
        )

    async def test_the_call_resolves_at_call_time(self, tmp_path):
        """The point of a name over a captured callable: disabling the wrapped
        extension makes the call fail, rather than running a dead extension."""
        session = _session()
        paths = await _load(session, tmp_path, a_ext=_A, w_ext=_WRAPPER)
        assert (await session.disable_extension(paths["a_ext"])).ok

        with pytest.raises(RuntimeError, match="no command named"):
            await session.run_extension_command("ext:w_ext.note", "x")


class TestPins:
    async def test_a_pin_beats_load_order(self, tmp_path):
        session = _session()
        session._registry.pin_command("note", "ext:b_ext.note")
        await _load(session, tmp_path, a_ext=_A, b_ext=_B)

        assert session._registry.get_bindings() == {"note": "ext:b_ext.note"}
        assert (await session.run_extension_command("note", "x")).output == "B:x"

    async def test_a_pin_survives_a_disable(self, tmp_path):
        session = _session()
        session._registry.pin_command("note", "ext:a_ext.note")
        paths = await _load(session, tmp_path, a_ext=_A, b_ext=_B)
        assert (await session.disable_extension(paths["a_ext"])).ok

        assert session._registry.get_bindings() == {}, "B does not inherit a pinned name"
        assert (await session.enable_extension(paths["a_ext"])).ok
        assert session._registry.get_bindings() == {"note": "ext:a_ext.note"}


_FLOW_A = """
from tau_agent_core.capabilities import Argument, Domain

def register(api):
    async def pick(args, ctx):
        return f"A picked {args}"
    api.register_flow(
        "pick", "A's pick", pick,
        argument=Argument("colour", "a_colour", "Which colour."),
        domain=Domain(
            name="a_colour",
            description="A's colours.",
            values=("red", "blue"),
            field_kind="select",
        ),
    )
"""

_FLOW_SHADOW = """
def register(api):
    async def pick(args, ctx):
        return f"B picked {args}"
    api.register_command("pick", {"description": "B's pick", "handler": pick})
"""


class TestAShadowedFlowIsNotInherited:
    """docs/EXTENSION-NAMESPACE.md §1, the fifth fault: the flow table was keyed by
    the typed name, so a shadowing command inherited the shadowed one's argument."""

    async def test_the_flow_follows_the_binding(self, tmp_path):
        session = _session()
        await _load(session, tmp_path, a_ext=_FLOW_A, b_ext=_FLOW_SHADOW)

        # A registered first, so /pick is A's and carries A's declaration.
        assert session.vocabulary.flow("pick").name == "ext:a_ext.pick"
        assert session.vocabulary.is_extension_flow("pick")

    async def test_the_shadowing_command_does_not_borrow_the_declaration(self, tmp_path):
        session = _session()
        paths = await _load(session, tmp_path, a_ext=_FLOW_A, b_ext=_FLOW_SHADOW)
        assert (await session.disable_extension(paths["a_ext"])).ok

        # /pick is now unbound; B's command is a plain one and declares nothing.
        assert session.vocabulary.flow("pick") is None
        assert session.vocabulary.flow("ext:b_ext.pick") is None
        assert (await session.run_extension_command("ext:b_ext.pick", "red")).output == (
            "B picked red"
        )

    async def test_an_argument_declared_by_one_is_not_offered_for_the_other(self, tmp_path):
        from tau_agent_core.commands import complete_command_argument

        session = _session()
        await _load(session, tmp_path, a_ext=_FLOW_A, b_ext=_FLOW_SHADOW)

        slot = complete_command_argument("/pick ", session.vocabulary)
        assert slot is not None and slot.argument == Argument(
            "colour", "a_colour", "Which colour."
        )
        assert complete_command_argument("/ext:b_ext.pick ", session.vocabulary) is None


class TestTheOneCollisionThatCanStillHappen:
    async def test_two_files_sharing_a_stem_are_refused(self, tmp_path):
        """Fail-Early, for register_tool's reason: which one survived would be
        decided by load order, silently."""
        first = tmp_path / "one" / "helper.py"
        second = tmp_path / "two" / "helper.py"
        for path in (first, second):
            path.parent.mkdir()
            path.write_text(_A)

        session = _session()
        result = await session.load_extensions(
            [str(first), str(second)], discover=False, collect_explicit_errors=True
        )
        assert len(result.errors) == 1
        assert "share the stem" in result.errors[0].error

    async def test_a_reload_is_not_a_collision(self, tmp_path):
        session = _session()
        paths = await _load(session, tmp_path, a_ext=_A)
        assert (await session.reload_extension(paths["a_ext"])).ok
        assert (await session.run_extension_command("note", "x")).output == "A:x"
